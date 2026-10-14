#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Rishon33. All rights reserved.
#
# Distributed under MIT license.
# See file LICENSE for detail or copy at https://opensource.org/licenses/MIT
"""把本地文件推送到 GitHub 仓库的一个分支上，可选打 tag 作为版本锚点。

为什么不用 git 命令
------------------
本机环境有几个坑，走 git CLI 会很烦：

1. 仓库带 git-lfs 过滤器（比如 *.tflite），本机没装 git-lfs，克隆会在
   checkout 阶段直接失败；
2. 全局 ``http.proxy`` 是 ``socks5h://``，而 Git Credential Manager 用的是
   .NET，不支持 socks5h，任何走凭证管理器的操作都会报
   "ServicePointManager 不支持具有 socks5h 方案的代理"；
3. 全局 git 没有配 user.name / user.email，每次 commit 都要显式指定。

本脚本直接调 GitHub 的 Git Data API（blobs -> trees -> commits -> refs），
用 urllib 走系统代理，只需要一个 token，不依赖本地克隆、不碰 LFS，
并且每个请求都带重试（本机代理偶发 502）。

换行符
------
``core.autocrlf=true`` 让 git 在提交时把 CRLF 归一化成 LF，但 Git Data API
是绕过 git 的，这层归一化不会发生。所以脚本自己做了等价处理：文本文件
CRLF -> LF，二进制文件原样。想跳过检测可以加 ``--binary``。

用法
----
    # 看一下会推什么，不写任何东西
    python push_to_fork.py observe_light.py --branch custom/observe-light --dry-run

    # 真实推送（分支不存在会自动从 --base 建）
    python push_to_fork.py observe_light.py \\
        --branch custom/observe-light \\
        --message "Add observe-light MCP extension" \\
        --tag observe-light-v1.0

    # 一次推多个文件，并改变它们在仓库里的落点
    python push_to_fork.py a.py b.py --dest tools/ \\
        --branch custom/tooling --message "Add tooling"

    # 查看当前分支和 tag 状态
    python push_to_fork.py --status

Token 解析顺序：``--token`` > 环境变量 ``GITHUB_TOKEN`` / ``GH_TOKEN`` >
``~/.workbuddy/mcp.json`` 里 ``mcpServers.github.headers.Authorization``。
``--token`` 会把令牌暴露在命令行里（可能被进程列表看到），非必要不要用。
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
DEFAULT_REPO = "Rishon33/lamda"
DEFAULT_BASE = "10"
DEFAULT_DEST = "examples/user-home/modules/extension"
DEFAULT_AUTHOR_NAME = "Rishon33"
UA = "wb-push-to-fork/1.0"

# 这些扩展名一律当二进制处理，不做换行归一化
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".so", ".dll", ".dylib", ".apk", ".aar", ".jar", ".class", ".dex",
    ".tflite", ".onnx", ".pt", ".pb", ".bin", ".exe", ".msi", ".woff", ".woff2",
    ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".pdf", ".sqlite", ".db",
}


class GithubError(Exception):
    def __init__(self, code, body=""):
        super().__init__("HTTP {0}: {1}".format(code, body[:300]))
        self.code = code
        self.body = body


# --------------------------------------------------------------------- http --
def api(method, path, payload=None, token=None, retries=4, timeout=30):
    """带重试的 GitHub REST 调用。4xx 里只有 404/409/422 直接抛给上层判断。"""
    url = API + path
    last = None
    for attempt in range(retries):
        try:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", "Bearer " + token)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "application/vnd.github+json")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (404, 409, 422):
                raise GithubError(exc.code, body)
            last = GithubError(exc.code, body)
        except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
            last = exc
        if attempt + 1 < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last


def resolve_token(cli_token=None):
    if cli_token:
        print("! 警告: --token 会出现在命令行参数里，可能被进程列表看到。", file=sys.stderr)
        return cli_token.strip()
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    config = Path.home() / ".workbuddy" / "mcp.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        header = data["mcpServers"]["github"]["headers"]["Authorization"]
        return header.split()[-1]
    except Exception:  # noqa: BLE001
        pass
    sys.exit("找不到 token：--token / $GITHUB_TOKEN / {0} 都没有。".format(config))


# ----------------------------------------------------------------- git data --
def blob_sha(data):
    """算出 git 的 blob 对象 id，用来判断远端内容是否已经一样。"""
    digest = hashlib.sha1()
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def normalize(path, binary_flag):
    """读文件并按 git 的 core.autocrlf=true 语义归一化。"""
    raw = Path(path).read_bytes()
    if binary_flag:
        return raw, False
    if Path(path).suffix.lower() in BINARY_EXT:
        return raw, False
    if b"\0" in raw[:8000]:  # 真正的二进制靠空字节 sniff，比看扩展名可靠
        return raw, False
    normalized = raw.replace(b"\r\n", b"\n")
    return normalized, normalized != raw


def ref_sha(repo, branch, token):
    """返回 (sha, exists)。"""
    try:
        obj = api("GET", "/repos/{0}/git/ref/heads/{1}".format(repo, branch), token=token)
        return obj["object"]["sha"], True
    except GithubError as exc:
        if exc.code == 404:
            return None, False
        raise


def remote_sha(repo, path, ref, token):
    try:
        obj = api("GET", "/repos/{0}/contents/{1}?ref={2}".format(repo, path, ref), token=token)
        if isinstance(obj, dict):
            return obj.get("sha")
    except GithubError as exc:
        if exc.code != 404:
            raise
    return None


def resolve_author(token, override):
    if override:
        name, _, email = override.partition("<")
        return {"name": name.strip(), "email": email.rstrip("> ").strip()}
    try:
        me = api("GET", "/user", token=token)
        login, uid = me["login"], me["id"]
        return {"name": login,
                "email": "{0}+{1}@users.noreply.github.com".format(uid, login)}
    except Exception:  # noqa: BLE001
        return {"name": DEFAULT_AUTHOR_NAME, "email": ""}


# ---------------------------------------------------------------------- main --
def build_plan(args):
    """把 (本地文件, 仓库路径) 配对，并算出归一化后的内容。"""
    dest = args.dest.rstrip("/") + "/"
    overrides = {}
    for pair in args.map or []:
        if "=" not in pair:
            sys.exit("--map 需要写成 本地文件=仓库路径 的形式: {0}".format(pair))
        local, remote = pair.split("=", 1)
        overrides[os.path.abspath(local)] = remote.lstrip("/")

    plan = []
    for item in args.files:
        local = Path(item)
        if not local.is_file():
            sys.exit("本地文件不存在: {0}".format(item))
        remote = overrides.get(os.path.abspath(local)) or (dest + local.name)
        data, converted = normalize(local, args.binary)
        plan.append({
            "local": str(local),
            "remote": remote,
            "data": data,
            "converted": converted,
            "sha": blob_sha(data),
        })
    return plan


def run(args):
    token = resolve_token(args.token)
    repo, base, branch = args.repo, args.base, args.branch

    # --status 是只读查询，先于任何写入门禁处理
    if args.status:
        print("repo: {0}".format(repo))
        branches = api("GET", "/repos/{0}/branches?per_page=100".format(repo), token=token)
        print("branches:", [b["name"] for b in branches])
        tags = api("GET", "/repos/{0}/tags?per_page=100".format(repo), token=token)
        print("tags:", [t["name"] for t in tags])
        return 0

    if not args.files:
        sys.exit("没有要推送的文件。加 --status 可以只看仓库状态。")

    if branch == base and not args.allow_default_branch:
        sys.exit(
            "拒绝提交到基线分支 {0}。自研内容请用专用分支，例如 "
            "--branch custom/xxx；确实要直接推到 {0} 就加 --allow-default-branch。".format(base))

    plan = build_plan(args)
    author = resolve_author(token, args.author)

    head_sha, branch_exists = ref_sha(repo, branch, token)
    if head_sha is None:
        head_sha, base_exists = ref_sha(repo, base, token)
        if head_sha is None:
            sys.exit("基线分支 {0} 也不存在，无法建立 {1}。".format(base, branch))
        print("分支 {0} 不存在，将从 {1} ({2}) 新建".format(branch, base, head_sha[:10]))
    else:
        print("分支 {0} 已存在 (tip {1})".format(branch, head_sha[:10]))

    print()
    changed = []
    for entry in plan:
        current = remote_sha(repo, entry["remote"], branch if branch_exists else base, token)
        if current == entry["sha"] and not args.force:
            print("  =  {0}  (内容已一致，跳过)".format(entry["remote"]))
            continue
        note = "CRLF->LF" if entry["converted"] else ""
        if current is None:
            note = (note + " " if note else "") + "新增"
        else:
            note = (note + " " if note else "") + "更新"
        print("  +  {0}  [{1}, {2} bytes]".format(entry["remote"], note, len(entry["data"])))
        changed.append(entry)

    if not changed:
        print("\n没有任何变化，未产生提交。")
        return 0

    if args.dry_run:
        print("\n--dry-run: 仅预览，未写入远端。")
        return 0

    parent_tree = api("GET", "/repos/{0}/git/commits/{1}".format(repo, head_sha),
                      token=token)["tree"]["sha"]

    tree_entries = []
    for entry in changed:
        blob = api("POST", "/repos/{0}/git/blobs".format(repo),
                   {"content": base64.b64encode(entry["data"]).decode("ascii"),
                    "encoding": "base64"}, token=token)
        tree_entries.append({"path": entry["remote"], "mode": "100644",
                             "type": "blob", "sha": blob["sha"]})

    tree = api("POST", "/repos/{0}/git/trees".format(repo),
               {"base_tree": parent_tree, "tree": tree_entries}, token=token)

    commit = api("POST", "/repos/{0}/git/commits".format(repo),
                 {"message": args.message, "tree": tree["sha"],
                  "parents": [head_sha], "author": author, "committer": author},
                 token=token)

    if branch_exists:
        api("PATCH", "/repos/{0}/git/refs/heads/{1}".format(repo, branch),
            {"sha": commit["sha"], "force": bool(args.force)}, token=token)
    else:
        api("POST", "/repos/{0}/git/refs".format(repo),
            {"ref": "refs/heads/{0}".format(branch), "sha": commit["sha"]}, token=token)

    print("\ncommit {0}".format(commit["sha"]))
    print("  https://github.com/{0}/commit/{1}".format(repo, commit["sha"]))
    print("  https://github.com/{0}/tree/{1}".format(repo, branch))

    if args.tag:
        tagger = {"name": author["name"], "email": author["email"]}
        message = args.tag_message or args.message
        try:
            tag_obj = api("POST", "/repos/{0}/git/tags".format(repo),
                          {"tag": args.tag, "message": message, "object": commit["sha"],
                           "type": "commit", "tagger": tagger}, token=token)
            api("POST", "/repos/{0}/git/refs".format(repo),
                {"ref": "refs/tags/{0}".format(args.tag), "sha": tag_obj["sha"]}, token=token)
            print("  tag {0} -> https://github.com/{1}/tree/{0}".format(args.tag, repo))
        except GithubError as exc:
            if exc.code == 422 and args.force:
                api("PATCH", "/repos/{0}/git/refs/tags/{1}".format(repo, args.tag),
                    {"sha": commit["sha"], "force": True}, token=token)
                print("  tag {0} 已存在，已强制移动".format(args.tag))
            else:
                print("  ! tag {0} 失败（可能已存在）: {1}".format(args.tag, exc), file=sys.stderr)
                return 1

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="通过 GitHub Git Data API 把本地文件推送到指定分支（免克隆、免 LFS、带重试）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="要推送的本地文件")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name，默认 " + DEFAULT_REPO)
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="分支不存在时从哪个分支派生，默认 " + DEFAULT_BASE)
    parser.add_argument("--branch", default=DEFAULT_BASE,
                        help="目标分支；默认等于 --base，脚本会拒绝直接提交到它")
    parser.add_argument("--dest", default=DEFAULT_DEST,
                        help="仓库内目标目录，默认 " + DEFAULT_DEST)
    parser.add_argument("--map", action="append", metavar="LOCAL=REMOTE",
                        help="单个文件改落点，可重复")
    parser.add_argument("--message", required=False, help="commit message")
    parser.add_argument("--message-file", help="从文件读 commit message")
    parser.add_argument("--tag", help="推送后额外打一个 annotated tag")
    parser.add_argument("--tag-message", help="tag message，默认复用 commit message")
    parser.add_argument("--author", metavar='"Name <email>"',
                        help="提交者，默认取 token 对应的 GitHub 账号")
    parser.add_argument("--token", help="直接给 token（会出现在命令行里，慎用）")
    parser.add_argument("--binary", action="store_true",
                        help="所有文件都不做 CRLF->LF 归一化")
    parser.add_argument("--force", action="store_true",
                        help="内容一致也提交；分支/tag 已存在时覆盖")
    parser.add_argument("--allow-default-branch", action="store_true",
                        help="允许直接提交到 --base 分支")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    parser.add_argument("--status", action="store_true", help="只打印分支与 tag 列表")
    args = parser.parse_args()

    if args.message_file:
        args.message = Path(args.message_file).read_text(encoding="utf-8")
    if not args.status and not args.message:
        parser.error("需要 --message（或 --message-file），或改用 --status")

    try:
        return run(args)
    except GithubError as exc:
        print("\nGitHub 报错: {0}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
