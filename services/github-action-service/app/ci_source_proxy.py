"""GitHub source archive proxy.

Downloads repository source tarballs via GitHub API and serves them to CI workers.
"""
import hashlib
import logging
import urllib.request
import urllib.error
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Preserve Authorization header when redirecting to codeload.github.com."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        parsed = urlparse(newurl)
        if "github" in parsed.netloc:
            auth = req.get_header("Authorization")
            if auth:
                new_req.add_unredirected_header("Authorization", auth)
        return new_req


def download_github_archive(repository: str, commit_sha: str, github_token: str, output_path: str) -> dict:
    """Download repository tarball from GitHub."""
    url = f"https://api.github.com/repos/{repository}/tarball/{commit_sha}"

    opener = urllib.request.build_opener(GitHubRedirectHandler())
    req = urllib.request.Request(url)
    req.add_header("Accept", "*/*")
    req.add_header("User-Agent", "github-action-service/1.0")
    req.add_header("Authorization", f"token {github_token}")

    sha256 = hashlib.sha256()
    total_bytes = 0

    try:
        with opener.open(req, timeout=180) as resp:
            with open(output_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    sha256.update(chunk)
                    f.write(chunk)

        return {
            "sha256": sha256.hexdigest(),
            "size_bytes": total_bytes,
        }
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        logger.error("GitHub download failed: HTTP %d: %s", e.code, body)
        raise ValueError(f"GitHub archive download failed: HTTP {e.code}: {body}")
    except Exception as e:
        logger.error("GitHub download error: %s", e)
        raise
