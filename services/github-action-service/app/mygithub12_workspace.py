"""MyGithut12 shared development workspace state."""
from __future__ import annotations
import json, os, re, sqlite3, uuid
from typing import Any
from app import mygithub12 as core

MyGithub12Error=core.MyGithub12Error
DEFAULT_LEASE_SECONDS=core.DEFAULT_LEASE_SECONDS
MAX_LEASE_SECONDS=core.MAX_LEASE_SECONDS
_now=core._now
_db=core._db
_LOCK=core._LOCK
_parse=core._parse
_safe_path=core._safe_path
_service_repo=core._service_repo
_tree_sha=core._tree_sha
resolve_identity=core.resolve_identity
get_index_status=core.get_index_status
_compare=core._compare
init_db=core.init_db

def _workspace_public(row: sqlite3.Row | dict[str,Any]) -> dict[str,Any]:
    r=dict(row); r["scope"]=json.loads(r.pop("scope_json") or "{}"); r["lease_valid"]=r["lease_expires_at"]>_now() and r["status"]=="active"; return r


def create_workspace(service: Any, repository: str, task_name: str, base_ref: str="main", branch: str="", owner: str="chatgpt", create_branch: bool=True, lease_seconds: int=DEFAULT_LEASE_SECONDS) -> dict[str,Any]:
    identity=resolve_identity(service,repository,ref=base_ref); slug=re.sub(r"[^a-z0-9-]+","-",task_name.lower()).strip("-")[:40] or "task"; workspace_id="ws_"+uuid.uuid4().hex[:16]; branch=branch or f"ai/{slug}-{workspace_id[-8:]}"
    if not branch.startswith("ai/"): raise MyGithub12Error("WORKSPACE_SCOPE_CONFLICT","workspace branches must use ai/ prefix")
    if create_branch:
        try: service.create_branch(repository,branch,base_ref)
        except Exception as exc: raise MyGithub12Error("WORKSPACE_LEASE_CONFLICT","workspace branch could not be created",{"branch":branch}) from exc
    branch_state=service.client.get_branch(repository,branch)
    if not branch_state: raise MyGithub12Error("WORKSPACE_NOT_FOUND","workspace branch does not exist")
    head=str(branch_state.commit.sha); tree=_tree_sha(_service_repo(service,repository).get_commit(head)); lease_seconds=max(60,min(lease_seconds,MAX_LEASE_SECONDS)); init_db(); now=_now()
    try:
        with _LOCK,_db() as db: db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(workspace_id,repository,branch,base_ref,identity["commit_sha"],head,tree,"active",1,owner,now+lease_seconds,head,"{}",None,None,now,now))
    except sqlite3.IntegrityError as exc: raise MyGithub12Error("WORKSPACE_LEASE_CONFLICT","another active workspace already owns this branch",{"branch":branch}) from exc
    return {"ok":True,**get_workspace(service,workspace_id),"index_reused":get_index_status(service,repository,head)["status"]=="ready"}


def get_workspace(service: Any, workspace_id: str) -> dict[str,Any]:
    init_db()
    with _db() as db: row=db.execute("SELECT * FROM workspaces WHERE workspace_id=?",(workspace_id,)).fetchone()
    if not row: raise MyGithub12Error("WORKSPACE_NOT_FOUND","workspace was not found",{"workspace_id":workspace_id})
    _service_repo(service,row["repository"]); return _workspace_public(row)


def list_workspaces(service: Any, repository: str="", status: str="", branch: str="", owner: str="", limit: int=50, offset: int=0) -> dict[str,Any]:
    if repository: _service_repo(service,repository)
    clauses=[]; values=[]
    for field,value in (("repository",repository),("status",status),("branch",branch),("owner",owner)):
        if value: clauses.append(field+"=?"); values.append(value)
    where=" WHERE "+" AND ".join(clauses) if clauses else ""; limit=max(1,min(limit,100)); offset=max(0,offset); init_db()
    with _db() as db:
        total=db.execute("SELECT COUNT(*) FROM workspaces"+where,values).fetchone()[0]; rows=db.execute("SELECT * FROM workspaces"+where+" ORDER BY updated_at DESC LIMIT ? OFFSET ?",(*values,limit,offset)).fetchall()
    return {"ok":True,"items":[_workspace_public(r) for r in rows],"total":total,"limit":limit,"offset":offset}


def _workspace_cas(workspace_id: str, expected: int) -> sqlite3.Row:
    with _db() as db: row=db.execute("SELECT * FROM workspaces WHERE workspace_id=?",(workspace_id,)).fetchone()
    if not row: raise MyGithub12Error("WORKSPACE_NOT_FOUND","workspace was not found")
    if int(row["revision"])!=int(expected): raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH","workspace revision changed",{"expected":expected,"actual":row["revision"]})
    return row


def renew_workspace_lease(service: Any, workspace_id: str, expected_workspace_revision: int, lease_seconds: int=DEFAULT_LEASE_SECONDS) -> dict[str,Any]:
    get_workspace(service,workspace_id); row=_workspace_cas(workspace_id,expected_workspace_revision)
    if row["status"]!="active": raise MyGithub12Error("WORKSPACE_CLOSED","workspace is not active")
    with _LOCK,_db() as db: db.execute("UPDATE workspaces SET lease_expires_at=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND revision=?",(_now()+max(60,min(lease_seconds,MAX_LEASE_SECONDS)),_now(),workspace_id,expected_workspace_revision))
    return {"ok":True,**get_workspace(service,workspace_id)}


def refresh_workspace(service: Any, workspace_id: str, expected_workspace_revision: int) -> dict[str,Any]:
    row=_workspace_cas(workspace_id,expected_workspace_revision); _service_repo(service,row["repository"]); branch=service.client.get_branch(row["repository"],row["branch"])
    if not branch: status,reason,head,tree="drifted","branch_deleted",row["head_sha"],row["tree_sha"]
    else:
        head=str(branch.commit.sha); tree=_tree_sha(_service_repo(service,row["repository"]).get_commit(head)); status="active" if head==row["head_sha"] else "drifted"; reason=None if status=="active" else "branch_moved_externally"
    with _LOCK,_db() as db: db.execute("UPDATE workspaces SET head_sha=?,tree_sha=?,status=?,drift_reason=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND revision=?",(head,tree,status,reason,_now(),workspace_id,expected_workspace_revision))
    return {"ok":True,**get_workspace(service,workspace_id)}


def close_workspace(service: Any, workspace_id: str, expected_workspace_revision: int) -> dict[str,Any]:
    get_workspace(service,workspace_id); _workspace_cas(workspace_id,expected_workspace_revision)
    with _LOCK,_db() as db: db.execute("UPDATE workspaces SET status='closed',lease_expires_at=0,index_commit_sha=NULL,revision=revision+1,updated_at=? WHERE workspace_id=? AND revision=?",(_now(),workspace_id,expected_workspace_revision))
    return {"ok":True,**get_workspace(service,workspace_id)}


def declare_workspace_scope(service: Any, workspace_id: str, expected_workspace_revision: int, paths_json: str="[]", symbols_json: str="[]", apis_json: str="[]", tables_json: str="[]", migrations_json: str="[]", configs_json: str="[]", exclusive: bool=False) -> dict[str,Any]:
    get_workspace(service,workspace_id); _workspace_cas(workspace_id,expected_workspace_revision); scope={"paths":[_safe_path(str(x)) for x in _parse(paths_json,list,"paths_json",[])],"symbols":_parse(symbols_json,list,"symbols_json",[]),"apis":_parse(apis_json,list,"apis_json",[]),"tables":_parse(tables_json,list,"tables_json",[]),"migrations":_parse(migrations_json,list,"migrations_json",[]),"configs":_parse(configs_json,list,"configs_json",[]),"exclusive":exclusive}
    with _LOCK,_db() as db: db.execute("UPDATE workspaces SET scope_json=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND revision=?",(json.dumps(scope),_now(),workspace_id,expected_workspace_revision))
    return {"ok":True,**get_workspace(service,workspace_id)}


def workspace_overlap(service: Any, workspace_id: str, other_workspace_ids_json: str="[]") -> dict[str,Any]:
    primary=get_workspace(service,workspace_id); requested=set(_parse(other_workspace_ids_json,list,"other_workspace_ids_json",[])); listing=list_workspaces(service,primary["repository"],"active")["items"]; others=[x for x in listing if x["workspace_id"]!=workspace_id and (not requested or x["workspace_id"] in requested)]; results=[]
    pscope=primary["scope"]
    for other in others:
        evidence=[]
        for key in ("paths","symbols","apis","tables","migrations","configs"):
            overlap=sorted(set(pscope.get(key,[])) & set(other["scope"].get(key,[])))
            if overlap: evidence.append({"kind":key,"items":overlap})
        try:
            cmp=_compare(service,primary["repository"],primary["base_commit_sha"],primary["head_sha"]); cmp2=_compare(service,other["repository"],other["base_commit_sha"],other["head_sha"]); actual=sorted({f["path"] for f in cmp["files"]}&{f["path"] for f in cmp2["files"]})
            if actual: evidence.append({"kind":"changed_paths","items":actual})
        except MyGithub12Error: pass
        level="high" if any(e["kind"] in {"changed_paths","migrations","tables","apis"} for e in evidence) else "medium" if evidence else "none"
        results.append({"workspace_id":other["workspace_id"],"branch":other["branch"],"level":level,"evidence":evidence})
    return {"ok":True,"workspace_id":workspace_id,"items":results}


def workspace_sync_plan(service: Any, workspace_id: str, base_branch: str="") -> dict[str,Any]:
    ws=get_workspace(service,workspace_id); base=base_branch or ws["base_branch"]; state=service.client.get_branch(ws["repository"],base)
    if not state: raise MyGithub12Error("REF_NOT_FOUND","base branch was not found")
    current=str(state.commit.sha); cmp=_compare(service,ws["repository"],current,ws["head_sha"]); changed={f["path"] for f in cmp["files"]}; base_cmp=_compare(service,ws["repository"],ws["base_commit_sha"],current) if current!=ws["base_commit_sha"] else {"files":[]}; base_changed={f["path"] for f in base_cmp["files"]}; overlap=sorted(changed&base_changed)
    return {"ok":True,"workspace_id":workspace_id,"branch":ws["branch"],"base_branch":base,"workspace_base_commit_sha":ws["base_commit_sha"],"current_base_commit_sha":current,"workspace_head_sha":ws["head_sha"],"base_advanced":current!=ws["base_commit_sha"],"overlapping_paths":overlap,"risk":"high" if overlap else "low","recommended_action":"manual_review" if overlap else "update_branch_or_merge"}


def workspace_write_preflight(service: Any, repository: str, branch: str, expected_head_sha: str, workspace_id: str="", expected_workspace_revision: int=0) -> dict[str,Any]:
    require=os.getenv("REQUIRE_WORKSPACE_FOR_AI_WRITES","false").lower() in {"1","true","yes","on"}
    if not workspace_id:
        if require and branch.startswith("ai/"): raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED","AI branch writes require a workspace")
        return {"workspace_id":None}
    if expected_workspace_revision<=0: raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH","expected_workspace_revision is required with workspace_id")
    ws=get_workspace(service,workspace_id)
    if ws["repository"]!=repository or ws["branch"]!=branch: raise MyGithub12Error("WORKSPACE_SCOPE_CONFLICT","workspace does not own the requested repository/branch")
    if ws["status"]!="active": raise MyGithub12Error("WORKSPACE_BRANCH_DRIFTED" if ws["status"]=="drifted" else "WORKSPACE_CLOSED","workspace is not writable")
    if not ws["lease_valid"]: raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED","workspace write lease expired")
    if ws["revision"]!=expected_workspace_revision: raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH","workspace revision changed",{"expected":expected_workspace_revision,"actual":ws["revision"]})
    branch_state=service.client.get_branch(repository,branch); actual=str(branch_state.commit.sha) if branch_state else ""
    if actual!=ws["head_sha"] or (expected_head_sha and actual!=expected_head_sha): raise MyGithub12Error("WORKSPACE_BRANCH_DRIFTED","branch HEAD differs from workspace",{"workspace_head":ws["head_sha"],"expected_head":expected_head_sha,"actual_head":actual})
    return ws


def workspace_write_complete(workspace_id: str, expected_workspace_revision: int, new_head_sha: str, tree_sha: str) -> dict[str,Any]:
    _workspace_cas(workspace_id,expected_workspace_revision)
    with _LOCK,_db() as db:
        cur=db.execute("UPDATE workspaces SET head_sha=?,tree_sha=?,index_commit_sha=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND revision=?",(new_head_sha,tree_sha,new_head_sha,_now(),workspace_id,expected_workspace_revision))
        if cur.rowcount!=1: raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH","workspace changed while completing write")
        row=db.execute("SELECT * FROM workspaces WHERE workspace_id=?",(workspace_id,)).fetchone()
    return _workspace_public(row)
