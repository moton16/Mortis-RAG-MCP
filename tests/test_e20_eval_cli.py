"""Independent GAP-OPS-01 CLI controls; frozen golden is never rewritten."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/eval/golden_queries.json"
FROZEN_SHA256 = "5a9c2823d03f7e17cf3308738d8005dcb4fbad789bf17b0307e52ef9f522d47a"
QUERY = "TTL 与非门的噪声容限怎么算"
BODY = (
    "# TTL 与非门\n\nTTL 与非门的噪声容限怎么算："
    "高电平噪声容限 V_NH = V_OHmin - V_IHmin，"
    "低电平噪声容限 V_NL = V_ILmax - V_OLmax。\n"
)


@pytest.mark.parametrize(
    "expect,expected_exit,mark,hit,mrr",
    [
        pytest.param("数电/ttl.md", 0, "HIT(#1)", "1/1 = 100.0%", "1.000", id="exact-source-hit"),
        pytest.param("数电/", 1, "MISS", "0/1 = 0.0%", "0.000", id="directory-prefix-miss"),
    ],
)
def test_eval_cli_exact_source_contract(tmp_path, expect, expected_exit, mark, hit, mrr):
    # 换行符不参与"未被改写"判定：同一个 golden 在 autocrlf=true 的检出里是 CRLF，
    # 在默认检出里是 LF（Windows CI 与本地/Ubuntu CI 实测字节不同），按原始字节哈希
    # 会让它自己成为唯一的平台相关性来源。统一折算成 LF 再哈希，冻结值仍等于 LF 形态。
    before = GOLDEN.read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha256(before).hexdigest() == FROZEN_SHA256
    assert json.loads(before)["queries"][0]["expect"] == "数电/"
    vault = tmp_path / "synthetic-vault"
    source = vault / "数电/ttl.md"
    source.parent.mkdir(parents=True)
    source.write_text(BODY, encoding="utf-8")
    source_before = source.read_bytes()
    generated = tmp_path / "synthetic-golden.json"
    generated.write_text(json.dumps({
        "synthetic_only": True,
        "queries": [{"vault": "./synthetic-vault", "query": QUERY,
                     "expect": expect, "path_prefix": "数电/"}],
    }, ensure_ascii=False), encoding="utf-8")
    config = tmp_path / "offline.toml"
    config.write_text(
        '[embedding]\nmode="static"\ndimension=8\n'
        '[reranker]\nenabled=false\n[ingest]\nenabled=false\n[cache]\nenabled=false\n',
        encoding="utf-8",
    )
    home, temp = tmp_path / "home", tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    registry = tmp_path / "registry.toml"
    registry.write_text("version=4\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MORTIS_", "VAULT_MCP", "MINERU_", "PYTHONPATH"))}
    env.update(
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
        PYTHONHASHSEED=os.environ.get("PYTHONHASHSEED", "0"),
        MORTIS_RAG_CONFIG=str(config), MORTIS_RAG_REGISTRY=str(registry),
        MORTIS_RAG_CACHE_DIR=str(tmp_path / "cache"), MORTIS_RAG_NO_STATUS_HOOK="1",
        HOME=str(home), USERPROFILE=str(home), APPDATA=str(home), LOCALAPPDATA=str(home),
        TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp),
    )
    # Execute the actual CLI main in a fresh interpreter, adding only a network tripwire.
    bootstrap = (
        "import runpy,sys\n"
        "def guard(event,args):\n"
        " if event in ('socket.connect','socket.connect_ex','socket.getaddrinfo',"
        "'socket.bind','socket.sendto'): raise AssertionError('E20_NETWORK_DISABLED')\n"
        "sys.addaudithook(guard)\n"
        "script=sys.argv.pop(1);sys.argv[0]=script\n"
        "runpy.run_path(script,run_name='__main__')\n"
    )
    command = [
        sys.executable, "-B", "-c", bootstrap, str(ROOT / "scripts/eval_search.py"),
        "--golden", str(generated), "--config", str(config), "--k", "5",
    ]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    record = dict(
        synthetic_only=True, command=command, command_shell=subprocess.list2cmdline(command),
        cwd=str(tmp_path), input=json.loads(generated.read_text(encoding="utf-8")),
        interpreter=sys.executable, version=sys.version, seed=env["PYTHONHASHSEED"],
        stdout=result.stdout, stderr=result.stderr, exit=result.returncode,
        frozen_golden_sha256=FROZEN_SHA256,
        eval_script_sha256=hashlib.sha256((ROOT / "scripts/eval_search.py").read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(source_before).hexdigest(),
    )
    run = os.environ.get("E20_RUN_DIR")
    if run:
        output = Path(run).resolve()
        output.relative_to((ROOT / ".runtime/beta2/E20").resolve())
        (output / ("cli-exact.json" if expected_exit == 0 else "cli-prefix.json")).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        assert result.returncode == expected_exit, record
        assert result.stderr == "", record
        assert f"[01/1] {mark}" in result.stdout, record
        assert f"expect {expect!r}" in result.stdout, record
        assert f"Hit@5: {hit}" in result.stdout, record
        assert f"MRR@5: {mrr}" in result.stdout, record
        if expected_exit:
            assert "top3=['数电/ttl.md']" in result.stdout, record
        else:
            assert "Misses:" not in result.stdout, record
        assert source.read_bytes() == source_before
    finally:
        assert GOLDEN.read_bytes() == before
