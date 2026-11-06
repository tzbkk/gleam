"""追加式证据存储：每次运行独占创建 UUID 目录，历史运行永不修改。

布局：
  <store>/runs/<UTC时间戳>-<uuid>/run.json          摘要（JSON）
  <store>/runs/<...>/report.md                      人审报告（Markdown）
  <store>/runs/<...>/observations/<obs_id>.json     单条观测记录
  <store>/runs/<...>/raw/<obs_id>.body|.headers     原始响应字节与响应头
  <store>/runs/<...>/raw/robots-<origin哈希>.txt     robots 原文（如抓到）
  <store>/runs/<...>/models/<call_id>.json          模型原始请求/响应
  <store>/index.json                                派生缓存：端点 → 最近一次成功观测
  <store>/index.lock                                index 读写互斥（flock）

baseline 规则：只与"同一端点上一次成功且未截断观测"比较；失败观测不成为
baseline。index.json 是可重建的缓存，不是证据：写入在 flock 下先重读再合并，
读取未命中时以全量扫描结果为准（防止过期缓存丢失最新基线）；check 子命令
做深度校验（逐条解析观测、核对原始 body 哈希、比对 index 内容）。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

# 标识符一律不含路径分隔符、前置点或盘符式冒号
SAFE_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
SUCCESS_OUTCOMES = ("OK", "NOT_MODIFIED", "REPRESENTATION_CHANGED")
KNOWN_OUTCOMES = SUCCESS_OUTCOMES + ("HTTP_ERROR", "TRANSPORT_ERROR",
                                     "ABSTAIN", "NOT_PROBED")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_id(identifier: str, what: str) -> str:
    if not isinstance(identifier, str) or not SAFE_ID.match(identifier) \
            or identifier in {".", ".."}:
        raise ValueError(f"unsafe {what}: {identifier!r}")
    return identifier


def _safe_join(base: str, identifier: str, what: str) -> str:
    _check_id(identifier, what)
    path = os.path.join(base, identifier)
    # 双保险：解析后必须仍落在 base 内（防任何分隔符/遍历绕过）
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(base):
        raise ValueError(f"{what} escapes store dir: {identifier!r}")
    return path


def _write_json(path: str, payload) -> None:
    tmp_fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".tmp-json-")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            # allow_nan=False：NaN/Inf 一律拒绝而非写成非法 JSON
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False)
            fh.write("\n")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _write_bytes(path: str, data: bytes) -> None:
    tmp_fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".tmp-bytes-")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


class RunStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.runs_root = os.path.join(self.root, "runs")
        self.run_id: str | None = None
        self.run_dir: str | None = None

    def list_runs(self) -> list[str]:
        if not os.path.isdir(self.runs_root):
            return []
        out = [d for d in sorted(os.listdir(self.runs_root))
               if os.path.isdir(os.path.join(self.runs_root, d))]
        return out

    def create_run(self) -> str:
        """独占创建运行目录；目录名冲突（概率性）时换 UUID 重试。"""

        os.makedirs(self.runs_root, exist_ok=True)
        for _ in range(5):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_id = f"{stamp}-{uuid.uuid4()}"
            path = os.path.join(self.runs_root, run_id)
            try:
                os.makedirs(path)  # exist_ok=False → 独占创建
            except FileExistsError:
                continue
            for sub in ("observations", "raw", "models"):
                os.makedirs(os.path.join(path, sub))
            self.run_id = run_id
            self.run_dir = path
            return run_id
        raise OSError("could not exclusively create run directory")

    # ---- 单次运行内的写入 -------------------------------------------------

    def _assert_run(self) -> str:
        if not self.run_dir or not self.run_id:
            raise RuntimeError("create_run() not called")
        _check_id(self.run_id, "run_id")
        return self.run_dir

    def write_observation(self, record: dict) -> None:
        obs_id = _check_id(record["observation_id"], "observation id")
        _write_json(_safe_join(os.path.join(self._assert_run(),
                                            "observations"),
                               obs_id, "observation id") + ".json", record)

    def write_raw(self, name: str, data: bytes) -> str:
        rel = f"raw/{name}"
        path = _safe_join(os.path.join(self._assert_run(), "raw"),
                          name, "raw name")
        _write_bytes(path, data)
        return rel

    def write_model_call(self, record: dict) -> None:
        call_id = _check_id(record["call_id"], "call id")
        _write_json(_safe_join(os.path.join(self._assert_run(), "models"),
                               call_id, "call id") + ".json", record)

    def write_summary(self, summary: dict, markdown: str) -> None:
        run_dir = self._assert_run()
        _write_json(os.path.join(run_dir, "run.json"), summary)
        _write_bytes(os.path.join(run_dir, "report.md"),
                     markdown.encode("utf-8"))

    # ---- baseline / index -------------------------------------------------

    def _index_path(self) -> str:
        return os.path.join(self.root, "index.json")

    @contextmanager
    def _index_lock(self):
        """index.json 的跨进程互斥；锁文件本身不是证据。"""

        os.makedirs(self.root, exist_ok=True)
        fd = os.open(os.path.join(self.root, "index.lock"),
                     os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load_index(self) -> dict:
        try:
            with open(self._index_path(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {"endpoints": {}}

    def _scan_endpoints(self) -> dict[str, dict]:
        """权威扫描：按 observed_at 取每端点最近一次成功且未截断的观测。"""

        endpoints: dict[str, dict] = {}
        for run_id in self.list_runs():
            obs_dir = os.path.join(self.runs_root, run_id, "observations")
            if not os.path.isdir(obs_dir):
                continue
            for fname in sorted(os.listdir(obs_dir)):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(obs_dir, fname), "r",
                              encoding="utf-8") as fh:
                        rec = json.load(fh)
                except (OSError, json.JSONDecodeError):
                    continue
                if rec.get("outcome") not in SUCCESS_OUTCOMES:
                    continue
                if rec.get("body_truncated"):
                    continue  # 截断 body 不作基线
                url = rec.get("endpoint", {}).get("url")
                if not url:
                    continue
                prev = endpoints.get(url)
                if prev is None or (rec.get("observed_at", "")
                                    >= prev.get("observed_at", "")):
                    endpoints[url] = _index_entry(rec)
        return endpoints

    def find_baseline(self, endpoint_url: str) -> dict | None:
        """优先 index；未命中时以权威全量扫描为准（并回写 index）。"""

        with self._index_lock():
            hit = self.load_index().get("endpoints", {}).get(endpoint_url)
            if hit:
                return hit
            endpoints = self._scan_endpoints()
            if endpoints:
                _write_json(self._index_path(),
                            {"rebuilt_at": utc_now(),
                             "endpoints": endpoints})
            return endpoints.get(endpoint_url)

    def rebuild_index(self) -> dict:
        with self._index_lock():
            endpoints = self._scan_endpoints()
            index = {"rebuilt_at": utc_now(), "endpoints": endpoints}
            _write_json(self._index_path(), index)
            return index

    def update_index(self, records: list[dict]) -> None:
        """把本次运行的成功观测并入 index（锁内先重读再合并，防丢更新）。"""

        with self._index_lock():
            index = self.load_index()
            for rec in records:
                if rec.get("outcome") not in SUCCESS_OUTCOMES:
                    continue
                if rec.get("body_truncated"):
                    continue  # 截断 body 不进基线
                url = rec.get("endpoint", {}).get("url")
                if not url:
                    continue
                index.setdefault("endpoints", {})[url] = _index_entry(rec)
            index["updated_at"] = utc_now()
            _write_json(self._index_path(), index)


def _index_entry(rec: dict) -> dict:
    return {
        "run_id": rec.get("run_id"),
        "observation_id": rec.get("observation_id"),
        "observed_at": rec.get("observed_at"),
        "outcome": rec.get("outcome"),
        "status_code": rec.get("status_code"),
        "content_sha256": rec.get("content_sha256"),
        "body_truncated": bool(rec.get("body_truncated")),
        "final_url": rec.get("final_url"),
    }


def verify_store(root: str) -> list[str]:
    """check 子命令：逐条深校验观测与原始件，并按内容比对 index。"""

    problems = []
    store = RunStore(root)
    runs = store.list_runs()
    if not runs:
        return ["no runs found (store is empty or missing)"]
    seen_ids = set()
    for run_id in runs:
        run_dir = os.path.join(store.runs_root, run_id)
        run_json = os.path.join(run_dir, "run.json")
        if not os.path.isfile(run_json):
            problems.append(f"{run_id}: run.json missing")
            continue
        try:
            with open(run_json, "r", encoding="utf-8") as fh:
                summary = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{run_id}: run.json unreadable ({exc})")
            continue
        if summary.get("run_id") != run_id:
            problems.append(f"{run_id}: run_id mismatch in run.json")
        run_scoped = set()
        for obs_id in summary.get("observation_ids", []):
            # observation id 只在单次运行内唯一；跨运行须带 run_id 前缀引用
            if obs_id in run_scoped:
                problems.append(f"{run_id}: duplicate observation id {obs_id}")
            run_scoped.add(obs_id)
            qualified = f"{run_id}/{obs_id}"
            if qualified in seen_ids:
                problems.append(f"duplicate observation reference {qualified}")
            seen_ids.add(qualified)
            obs_path = os.path.join(run_dir, "observations",
                                    f"{obs_id}.json")
            if not os.path.isfile(obs_path):
                problems.append(f"{run_id}: observation {obs_id} file missing")
                continue
            try:
                with open(obs_path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"{run_id}: observation {obs_id} corrupt ({exc})")
                continue
            if rec.get("observation_id") != obs_id:
                problems.append(
                    f"{run_id}: observation file {obs_id}.json has id "
                    f"{rec.get('observation_id')!r}")
            if rec.get("run_id") != run_id:
                problems.append(
                    f"{run_id}: observation {obs_id} claims run_id "
                    f"{rec.get('run_id')!r}")
            if rec.get("outcome") not in KNOWN_OUTCOMES:
                problems.append(
                    f"{run_id}: observation {obs_id} unknown outcome "
                    f"{rec.get('outcome')!r}")
            if not isinstance(rec.get("endpoint"), dict):
                problems.append(
                    f"{run_id}: observation {obs_id} missing endpoint object")
            # body（HTTP 观测）或 csv（fetch-udd 的 psql --csv 输出）都
            # 是该观测的载荷：check 对两者同样重算哈希
            raw_refs = rec.get("raw_refs") or {}
            body_ref = raw_refs.get("body") or raw_refs.get("csv")
            sha = rec.get("content_sha256")
            if body_ref and sha and not rec.get("effective_hash_from_baseline"):
                body_path = os.path.join(run_dir, body_ref)
                try:
                    with open(body_path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    problems.append(
                        f"{run_id}: observation {obs_id} raw body missing")
                else:
                    if hashlib.sha256(data).hexdigest() != sha:
                        problems.append(
                            f"{run_id}: observation {obs_id} raw body hash "
                            f"mismatch (corrupted?)")
                    if isinstance(rec.get("content_bytes"), int) \
                            and rec["content_bytes"] != len(data) \
                            and rec.get("status_code") != 304:
                        problems.append(
                            f"{run_id}: observation {obs_id} content_bytes "
                            f"{rec['content_bytes']} != raw size {len(data)}")
    # index 按内容比对（不只比数量）；键取旧/新并集：旧 index 中的端点若
    # 其观测已损坏或文件缺失，重建后会从新 index 消失，同样属于 out-of-sync
    index = store.load_index()
    rebuilt = store.rebuild_index()
    old_eps = index.get("endpoints") or {}
    new_eps = rebuilt.get("endpoints") or {}
    if old_eps and old_eps != new_eps:
        differing = sorted(url for url in old_eps.keys() | new_eps.keys()
                           if old_eps.get(url) != new_eps.get(url))
        example = differing[0] if differing else "?"
        problems.append(
            f"index.json content out of sync for {len(differing)} endpoint(s) "
            f"(e.g. {example}); rewritten by this check")
    return problems
