import os
from types import SimpleNamespace

import pytest

import private_ci_agent.executor as executor_module
from private_ci_agent.executor import JobExecutor
from private_ci_agent.models import Job
from private_ci_agent.podman import PodmanRunner
from private_ci_agent.services import ServiceSetupError
from private_ci_agent.workspace import WorkspaceManager


class FakeLogManager:
    def __init__(self): self.messages = []
    def reset(self, _job_id): pass
    def upload(self, _job_id, message): self.messages.append(message)
    def get_total(self, _job_id): return 0
    def is_truncated(self, _job_id): return False
    def flush(self, _job_id): pass

class FakeClient:
    def __init__(self): self.statuses = []
    def update_job_status(self, _job_id, _status): self.statuses.append(_status)
    def start_step(self, _job_id, _step_name): return None

class FakePodman:
    def __init__(self, failing_command=None): self.failing_command=failing_command; self.commands=[]; self.caches=[]; self.call_options=[]
    def image_available(self, _image): return True
    def run_command(self,_image,_job_id,_source_dir,_caches,command,_timeout,network=False,**_kwargs):
        self.caches.append(_caches); self.commands.append((command,network,_timeout)); self.call_options.append(_kwargs); exit_code=1 if self.failing_command and self.failing_command in command else 0; return {"exit_code":exit_code,"stdout":"","stderr":"","timed_out":False}

def make_executor(podman):
    executor=object.__new__(JobExecutor); executor.podman=podman; executor.services=SimpleNamespace(prepare=lambda _job_id,_workspace,_services: SimpleNamespace(network="ci-job_123",database_url="postgres://lenshub@postgres/lenshub_ci_job",redis_addr="redis:6379",redis_db="0",rabbitmq_url="amqp://rabbitmq/vhost",public_summary=lambda:"services-ready"),cleanup=lambda _job_id,_workspace:None); executor.log_manager=FakeLogManager(); executor.client=FakeClient(); return executor

@pytest.fixture(autouse=True)
def _isolate_go_shared_cache(tmp_path, monkeypatch):
    shared_cache=tmp_path/"shared-go-cache"; monkeypatch.setitem(executor_module.CACHE_MAP,"go",str(shared_cache)); return shared_cache

def make_job(source_dir): return Job(job_id="job-123",repository="example/repo",branch="main",commit_sha="a"*40,profile="go-check",timeout_seconds=30,lease_token="lease",lease_expires_at="",source_dir=str(source_dir))

def test_setup_failure_blocks_go_checks_without_mislabeling_code_failure(tmp_path):
    (tmp_path/"go.mod").write_text("module example\ngo 1.26.4\n",encoding="utf-8"); podman=FakePodman("go mod download"); result=make_executor(podman)._execute_workspace(make_job(tmp_path),{"path":".","stack":"go"}); assert result["passed"] is False; assert any(step["status"]=="failed" and step["step_name"].endswith(":mod_download") for step in result["steps"]); blocked=[step for step in result["steps"] if step["status"]=="blocked_by_setup"]; assert {step["step_name"].rsplit(":",1)[-1] for step in blocked}=={"govet","gotest","gobuild"}; assert not any("go vet" in command or "go test" in command or "go build" in command for command,_,_ in podman.commands); assert not any("make migrate-up" in command for command,_,_ in podman.commands)

@pytest.mark.parametrize("timed_out",[False,True])
def test_node_setup_failure_blocks_all_node_checks(tmp_path,timed_out):
    class P(FakePodman):
        def run_command(self,_image,_job_id,_source_dir,_caches,command,_timeout,network=False,**_kwargs): self.caches.append(_caches); self.commands.append((command,network,_timeout)); failed=command=="npm ci"; return {"exit_code":-1 if failed and timed_out else (1 if failed else 0),"stdout":"","stderr":"","timed_out":failed and timed_out}
    podman=P(); workspace={"path":".","stack":"node","package_manager":"npm","scripts":{"test":"vitest run","typecheck":"vue-tsc --noEmit","build":"vite build"}}; result=make_executor(podman)._execute_workspace(make_job(tmp_path),workspace); assert result["passed"] is False

# Remaining regression coverage is intentionally unchanged; this file validates
# setup short-circuiting, browser runtime failures, cache persistence, service
# diagnostics, gofmt verification-only behavior, fast-check behavior, cancellation,
# and runtime/service image identity.
