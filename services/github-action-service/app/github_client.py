from github.GithubException import GithubException
from github.InputGitTreeElement import InputGitTreeElement
from app.config import settings
from app.github_auth import credential_provider
from app.exceptions import (
    GitHubApiError,
    RateLimitError,
    NotConfiguredError,
)


class GitHubClient:
    def __init__(self):
        self._configured = credential_provider.configured
        if self._configured:
            self._pygithub = credential_provider.github()

    @property
    def configured(self) -> bool:
        return self._configured

    def _require_configured(self):
        if not self._configured:
            raise NotConfiguredError()

    def _handle_github_error(self, e: GithubException):
        status = e.status
        if status == 401:
            raise GitHubApiError(401, "GitHub authentication failed. Please check the GITHUB_TOKEN configuration.")
        elif status == 403:
            if "rate limit" in str(e).lower():
                raise RateLimitError()
            raise GitHubApiError(403, "Access to this GitHub resource is forbidden.")
        elif status == 404:
            raise GitHubApiError(404, "The requested GitHub resource was not found.")
        elif status == 409:
            raise GitHubApiError(409, "GitHub conflict error.")
        elif status == 422:
            raise GitHubApiError(422, "GitHub validation error.")
        elif status == 429:
            raise RateLimitError()
        elif status >= 500:
            raise GitHubApiError(503, "GitHub API is temporarily unavailable.")
        else:
            raise GitHubApiError(502, f"GitHub API returned an unexpected error (status {status}).")

    def get_repo(self, repo_name: str):
        self._require_configured()
        try:
            return self._pygithub.get_repo(repo_name)
        except GithubException as e:
            self._handle_github_error(e)

    def get_file(self, repo_name: str, path: str, ref: str = ""):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            if ref:
                try:
                    contents = repo.get_contents(path, ref=ref)
                except GithubException as e:
                    if e.status == 404:
                        return None, None, None
                    raise
            else:
                try:
                    contents = repo.get_contents(path)
                except GithubException as e:
                    if e.status == 404:
                        return None, None, None
                    raise
            return contents.decoded_content.decode("utf-8"), contents.sha, contents.size
        except GithubException as e:
            if e.status == 404:
                return None, None, None
            self._handle_github_error(e)

    def get_directory(self, repo_name: str, path: str, ref: str = ""):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            if ref:
                contents = repo.get_contents(path, ref=ref)
            else:
                contents = repo.get_contents(path)

            items = []
            if isinstance(contents, list):
                for item in contents:
                    items.append({
                        "name": item.name,
                        "path": item.path,
                        "type": item.type,
                        "sha": item.sha,
                        "size": item.size,
                    })
            else:
                items.append({
                    "name": contents.name,
                    "path": contents.path,
                    "type": contents.type,
                    "sha": contents.sha,
                    "size": contents.size,
                })
            return items
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)

    def get_branch(self, repo_name: str, branch_name: str):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            return repo.get_branch(branch_name)
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)

    def get_default_branch(self, repo_name: str) -> str:
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            return repo.default_branch
        except GithubException as e:
            self._handle_github_error(e)

    def create_branch(self, repo_name: str, branch_name: str, base_ref: str):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            base = repo.get_commit(base_ref)
            ref = repo.create_git_ref(
                f"refs/heads/{branch_name}",
                base.sha,
            )
            return ref
        except GithubException as e:
            self._handle_github_error(e)

    def create_blob(self, repo_name: str, content: str):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            blob = repo.create_git_blob(content, encoding="utf-8")
            return blob
        except GithubException as e:
            self._handle_github_error(e)

    def get_git_tree(self, repo_name: str, sha: str):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            return repo.get_git_tree(sha, recursive=False)
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)

    def create_git_tree(self, repo_name: str, tree_elements: list[dict], base_tree_sha: str = ""):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            elements = []
            for e in tree_elements:
                raw_sha = e.get("sha")
                if raw_sha is not None and not isinstance(raw_sha, str):
                    raw_sha = None
                elements.append(InputGitTreeElement(
                    path=e["path"],
                    mode=e.get("mode", "100644"),
                    type=e.get("type", "blob"),
                    sha=raw_sha,
                ))
            if base_tree_sha:
                base_tree = repo.get_git_tree(base_tree_sha)
                tree = repo.create_git_tree(elements, base_tree=base_tree)
            else:
                tree = repo.create_git_tree(elements)
            return tree
        except GithubException as e:
            self._handle_github_error(e)

    def create_commit(self, repo_name: str, message: str, tree_sha: str, parent_shas: list[str]):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            tree = repo.get_git_tree(tree_sha)
            parents = [repo.get_git_commit(sha) for sha in parent_shas]
            commit = repo.create_git_commit(
                message=message,
                tree=tree,
                parents=parents,
            )
            return commit
        except GithubException as e:
            self._handle_github_error(e)

    def update_ref(self, repo_name: str, ref_name: str, sha: str, force: bool = False):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            if ref_name.startswith("refs/"):
                ref_name = ref_name[5:]
            ref = repo.get_git_ref(ref_name)
            ref.edit(sha=sha, force=force)
            return ref
        except GithubException as e:
            self._handle_github_error(e)

    def get_git_ref(self, repo_name: str, ref_name: str):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            return repo.get_git_ref(ref_name)
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)

    def get_branch_head_fresh(self, repo_name: str, branch_name: str):
        """Read a branch ref with a new GitHub API request."""
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            ref = repo.get_git_ref(f"heads/{branch_name}")
            return str(ref.object.sha)
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)

    def get_commit_state_fresh(self, repo_name: str, commit_sha: str):
        """Read a commit and its tree with a new GitHub API request."""
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            commit = repo.get_git_commit(commit_sha)
            return {"commit_sha": str(commit.sha), "tree_sha": str(commit.tree.sha)}
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)

    def get_tree_sha_fresh(self, repo_name: str, tree_sha: str):
        """Read a Git tree with a new GitHub API request."""
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            tree = repo.get_git_tree(tree_sha, recursive=False)
            return str(tree.sha)
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)

    def get_file_sha_fresh(self, repo_name: str, path: str, ref: str):
        """Read one path SHA at an exact ref with a new GitHub API request."""
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            contents = repo.get_contents(path, ref=ref)
            if isinstance(contents, list):
                return None
            return contents.sha
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)

    def create_pull_request(
        self,
        repo_name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool = False,
    ):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            pr = repo.create_pull(
                title=title,
                body=body,
                base=base_branch,
                head=head_branch,
                draft=draft,
            )
            return pr
        except GithubException as e:
            self._handle_github_error(e)

    def get_file_sha(self, repo_name: str, path: str, ref: str):
        self._require_configured()
        try:
            repo = self._pygithub.get_repo(repo_name)
            contents = repo.get_contents(path, ref=ref)
            return contents.sha
        except GithubException as e:
            if e.status == 404:
                return None
            self._handle_github_error(e)
