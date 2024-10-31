import json
import os
from typing import List, Dict, Any
import google.generativeai as Client
from github import Github
import difflib
import requests
import fnmatch
from unidiff import Hunk, PatchedFile, PatchSet

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# Initialize GitHub and Gemini clients
gh = Github(GITHUB_TOKEN)
gemini_client = Client.configure(api_key=os.environ.get('GEMINI_API_KEY'))


class PRDetails:
    def __init__(self, owner: str, repo: str, pull_number: int, title: str, description: str):
        self.owner = owner
        self.repo = repo
        self.pull_number = pull_number
        self.title = title
        self.description = description


def get_pr_details() -> PRDetails:
    """Retrieves details of the pull request from GitHub Actions event payload."""
    with open(os.environ["GITHUB_EVENT_PATH"], "r") as f:
        event_data = json.load(f)
    repo_full_name = event_data["repository"]["full_name"]
    owner, repo = repo_full_name.split("/")
    pull_number = event_data["number"]

    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pull_number)

    return PRDetails(owner, repo.name, pull_number, pr.title, pr.body)


def get_diff(owner: str, repo: str, pull_number: int) -> str:
    """Fetches the diff of the pull request from GitHub API."""
    repo = gh.get_repo(f"{owner}/{repo}")
    pr = repo.get_pull(pull_number)
    
    # Get the diff using the diff_url
    headers = {'Authorization': f'token {GITHUB_TOKEN}'}
    response = requests.get(pr.diff_url, headers=headers)
    
    if response.status_code == 200:
        diff = response.text
        print(f"Retrieved diff length: {len(diff) if diff else 0}")
        return diff
    else:
        print(f"Failed to get diff. Status code: {response.status_code}")
        return ""


def analyze_code(parsed_diff: List[Dict[str, Any]], pr_details: PRDetails) -> List[Dict[str, Any]]:
    """Analyzes the code changes using Gemini and generates review comments."""
    print("Starting analyze_code...")
    comments = []
    print(f"Initial comments list: {comments}")
    
    for file_data in parsed_diff:
        file_path = file_data.get('path', '')
        if not file_path or file_path == "/dev/null":
            continue  # Skip files without path or deleted files
            
        print(f"Processing file: {file_path}")
        
        # Create PatchedFile object
        patched_file = PatchedFile(
            source_file=f"a/{file_path}",
            target_file=f"b/{file_path}"
        )
        patched_file.path = file_path  # Set the path explicitly
        
        for hunk_data in file_data.get('hunks', []):
            hunk_lines = hunk_data.get('lines', [])
            if not hunk_lines:
                continue
                
            # Create Hunk object
            hunk = Hunk()
            hunk.source_start = 1
            hunk.source_length = len(hunk_lines)
            hunk.target_start = 1
            hunk.target_length = len(hunk_lines)
            hunk.content = '\n'.join(hunk_lines)
            
            prompt = create_prompt(patched_file, hunk, pr_details)
            print("Sending prompt to Gemini...")
            ai_response = get_ai_response(prompt)
            print(f"Received AI response: {ai_response}")
            
            if ai_response:
                new_comments = create_comment(patched_file, hunk, ai_response)
                if new_comments:
                    comments.extend(new_comments)
                    print(f"Updated comments after adding new ones: {comments}")
                    
    return comments


def create_prompt(file: PatchedFile, hunk: Hunk, pr_details: PRDetails) -> str:
    """Creates the prompt for the Gemini model."""
    return f"""Your task is reviewing pull requests. Instructions:
    - Provide the response in following JSON format:  {{"reviews": [{{"lineNumber":  <line_number>, "reviewComment": "<review comment>"}}]}}
    - Do not give positive comments or compliments.
    - Provide comments and suggestions ONLY if there is something to improve, otherwise "reviews" should be an empty array.
    - Write the comment in GitHub Markdown format.
    - Use the given description only for the overall context and only comment the code.
    - IMPORTANT: NEVER suggest adding comments to the code.

Review the following code diff in the file "{file.path}" and take the pull request title and description into account when writing the response.
  
Pull request title: {pr_details.title}
Pull request description:

---
{pr_details.description}
---

Git diff to review:

```diff
{hunk.content}
{chr(10).join([f"{c.ln if c.ln else c.ln2} {c.content}" for c in hunk.changes])}
```
"""

def get_ai_response(prompt: str) -> List[Dict[str, str]]:
    """Sends the prompt to Gemini API and retrieves the response."""
    print("===== The promt sent to Gemini is: =====")
    print(prompt)
    try:
        response = gemini_client.generate_text(
            prompt=prompt,
            model="gemini-1.5-pro-002",
            temperature=0.2,
            max_output_tokens=700,
        )
        print(f"Raw Gemini response: {response.result}")  # Print raw response
        prompt += "\nPlease format your response as a JSON object with a 'reviews' array containing objects with 'lineNumber' and 'reviewComment' fields."

        try:
            data = json.loads(response.result.strip())
            if "reviews" in data and isinstance(data["reviews"], list):
                reviews = data["reviews"]
                # Validate each review item
                valid_reviews = []
                for review in reviews:
                    if "lineNumber" in review and "reviewComment" in review:
                        valid_reviews.append(review)
                    else:
                        print(f"Invalid review format: {review}")
                return valid_reviews
            else:
                print("Error: Response doesn't contain valid 'reviews' array")
                print(f"Response content: {data}")
                return []
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON response: {e}")
            print(f"Raw response: {response.result}")
            return []
    except Exception as e:
        print(f"Error during Gemini API call: {e}")
        return []

def create_comment(file: PatchedFile, hunk: Hunk, ai_responses: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Creates comment objects from AI responses."""
    print("AI responses in create_comment:", ai_responses)
    comments = []
    for ai_response in ai_responses:
        try:
            line_number = hunk.source_start + int(ai_response["lineNumber"]) - 1
            print(f"Creating comment for line: {line_number}")  # Debugging print
            comments.append({
                "body": ai_response["reviewComment"],
                "path": file.path,
                "line": line_number,
            })
        except (KeyError, TypeError, ValueError) as e:  # Catch ValueError for line number conversion
            print(f"Error creating comment from AI response: {e}, Response: {ai_response}")
    return comments

def create_review_comment(
    owner: str,
    repo: str,
    pull_number: int,
    comments: List[Dict[str, Any]],
):
    """Submits the review comments to the GitHub API."""
    print(f"Attempting to create {len(comments)} review comments")
    print(f"Comments content: {json.dumps(comments, indent=2)}")

    repo = gh.get_repo(f"{owner}/{repo}")
    pr = repo.get_pull(pull_number)
    try:
        review = pr.create_review(comments=comments, event="COMMENT")
        print(f"Review created successfully: {review}")
    except Exception as e:
        print(f"Error creating review: {str(e)}")
        print(f"Review payload: {comments}")

def parse_diff(diff_str: str) -> List[Dict[str, Any]]:
    """Parses the diff string and returns a structured format."""
    files = []
    current_file = None
    current_hunk = None
    
    for line in diff_str.splitlines():
        if line.startswith('diff --git'):
            if current_file:
                files.append(current_file)
            current_file = {'path': '', 'hunks': []}
            
        elif line.startswith('--- a/'):
            if current_file:
                current_file['path'] = line[6:]
                
        elif line.startswith('+++ b/'):
            if current_file:
                current_file['path'] = line[6:]
                
        elif line.startswith('@@'):
            if current_file:
                current_hunk = {'header': line, 'lines': []}
                current_file['hunks'].append(current_hunk)
                
        elif current_hunk is not None:
            current_hunk['lines'].append(line)
            
    if current_file:
        files.append(current_file)
        
    return files



def main():
    """Main function to execute the code review process."""
    pr_details = get_pr_details()
    event_data = json.load(open(os.environ["GITHUB_EVENT_PATH"], "r"))
    if event_data["action"] == "opened":
        diff = get_diff(pr_details.owner, pr_details.repo, pr_details.pull_number)
        print("===== Diff =====:", diff)
        if not diff:
            print("No diff found")
            return

        parsed_diff = parse_diff(diff)

        exclude_patterns = os.environ.get("INPUT_EXCLUDE", "").split(",")
        exclude_patterns = [s.strip() for s in exclude_patterns]

        filtered_diff = [
            file
            for file in parsed_diff
            if not any(fnmatch.fnmatch(file.get('path', ''), pattern) for pattern in exclude_patterns)
        ]
        print(f"Filtered diff, number of files: {len(filtered_diff)}")

        comments = analyze_code(filtered_diff, pr_details)
        if comments:
            create_review_comment(
                pr_details.owner, pr_details.repo, pr_details.pull_number, comments
            )
    elif event_data["action"] == "synchronize":
        diff = get_diff(pr_details.owner, pr_details.repo, pr_details.pull_number)
        print("===== Diff =====:", diff)
        if not diff:
            print("No diff found")
            return

        parsed_diff = parse_diff(diff)

        exclude_patterns = os.environ.get("INPUT_EXCLUDE", "").split(",")
        exclude_patterns = [s.strip() for s in exclude_patterns]

        filtered_diff = [
            file
            for file in parsed_diff
            if not any(fnmatch.fnmatch(file.get('path', ''), pattern) for pattern in exclude_patterns)
        ]

        comments = analyze_code(filtered_diff, pr_details)
        print("========== There are some comments on the PR ==========")
        print(comments)
        if comments:
            try:
                create_review_comment(
                    pr_details.owner, pr_details.repo, pr_details.pull_number, comments
                )
                print("***** Create-Alex-Comment *****")  # Debug print
            except Exception as e:
                print("Error in create_review_comment:", e)
    else:
        print("Unsupported event:", os.environ.get("GITHUB_EVENT_NAME"))
        return


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Error:", error)
