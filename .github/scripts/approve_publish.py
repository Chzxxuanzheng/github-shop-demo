#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「审核通过」的组件收录进 items.json。

与 ``check_publish.py`` 的分工:

    check_publish.py    —— Issue 创建/编辑时跑自动化校验，只读，不改仓库。
    approve_publish.py  —— Issue 被打上「审核通过」标签时跑，**会写** items.json
                           并提交推送，属于真正的「上架」动作。

收录内容 = 组件仓库里的 ``meta.json`` 全量字段 + 来源信息::

    {
        "id": "demo",
        "name": "组件demo",
        "desc": "一个用于测试的东西啦",
        "author": "Mr.Lee",
        "enter": "./src/main.py",
        "repo": "https://github.com/owner/repo",
        "issue": 12,
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "added_at": "2026-11-01T02:00:00+00:00"
    }

脚本行为:
    1. 从 Issue 正文里解析组件仓库地址（也可用 ``--repo-url`` 直接指定）；
    2. 浅克隆组件仓库，读取并校验 ``meta.json``；
    3. 按 ``id`` 判断是否已收录：同一仓库重复触发则**更新**该条记录（幂等），
       其它仓库已占用同一 ``id`` 则直接失败，不覆盖别人的组件；
    4. 写回 ``items.json``（保持原有结构：数组 / {"items": [...]} / 以 id 为键的对象）；
    5. git commit + push，推送失败时从远端最新内容重新合并后重试；
    6. 把结果渲染成一条评论回写到 Issue（同一个 Issue 只保留一条收录评论）。

退出码: 0 表示收录成功（或本来就是最新状态），1 表示失败。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# 允许直接 `python3 .github/scripts/approve_publish.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_publish as base  # noqa: E402  （依赖上面的 sys.path 调整）

# 收录结果直接覆盖校验 CI 发的那条评论（靠同一个隐藏标记定位），不新建评论
APPROVE_COMMENT_MARKER = base.COMMENT_MARKER

# meta.json 之外额外写入的来源信息字段
SOURCE_FIELDS = ('repo', 'issue', 'revision', 'added_at', 'updated_at')

# 推送失败后重新合并并重试的最大次数
PUSH_ATTEMPTS = 3

# git 命令超时（秒）
GIT_TIMEOUT = 180


def log(message: str) -> None:
	"""输出到 stderr，与 check_publish 保持一致。"""
	print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# items.json 读写
# --------------------------------------------------------------------------- #
def dump_items(data: Any) -> str:
	"""把 items.json 的内容序列化回文本。"""
	return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def append_record(data: Any, record: dict[str, Any]) -> None:
	"""按 items.json 原有结构追加一条记录。"""
	if isinstance(data, list):
		data.append(record)
		return

	if isinstance(data, dict):
		inner = data.get('items')
		if isinstance(inner, list):
			inner.append(record)
			return
		if isinstance(inner, dict):
			inner[record['id']] = record
			return
		# 以 id 为键的扁平对象
		data[record['id']] = record
		return

	raise ValueError('items.json 顶层必须是数组或对象')


def merge_record(
	items_path: Path, record: dict[str, Any], dry_run: bool = False
) -> tuple[str, str]:
	"""把一条组件记录合并进 items.json。

	返回 ``(动作, 错误信息)``，动作为 ``added`` / ``updated`` / ``unchanged``。
	错误信息非空时表示失败，此时不会写文件。
	``dry_run=True`` 时只计算动作，不落盘。
	"""
	if items_path.is_file():
		raw = items_path.read_text(encoding='utf-8').strip()
	else:
		raw = ''

	try:
		data: Any = json.loads(raw) if raw else []
	except json.JSONDecodeError as exc:
		return (
			'',
			f'`{items_path.name}` 不是合法的 JSON: {exc.msg} (第 {exc.lineno} 行)',
		)

	if not isinstance(data, (list, dict)):
		return '', f'`{items_path.name}` 顶层必须是数组或对象'

	new_id = str(record['id']).strip()
	action = 'added'

	for existing in base.extract_items(data):
		if str(existing.get('id') or '').strip() != new_id:
			continue

		existing_repo = base.normalize_repo_url(
			str(existing.get('repo') or existing.get('url') or '')
		)
		if existing_repo and existing_repo != record['repo']:
			return (
				'',
				f'`id` = `{new_id}` 已被 `{existing_repo}` 收录，拒绝覆盖其它组件。',
			)

		# 同一仓库重复触发：保留首次收录时间，刷新其余字段
		merged = dict(existing)
		merged.update(record)
		if existing.get('added_at'):
			merged['added_at'] = existing['added_at']
		if existing == merged:
			return 'unchanged', ''

		existing.clear()
		existing.update(merged)
		action = 'updated'
		break
	else:
		try:
			append_record(data, record)
		except ValueError as exc:
			return '', str(exc)

	if dry_run:
		return action, ''

	items_path.parent.mkdir(parents=True, exist_ok=True)
	items_path.write_text(dump_items(data), encoding='utf-8')
	return action, ''


# --------------------------------------------------------------------------- #
# git 操作
# --------------------------------------------------------------------------- #
def _git(
	args: list[str], cwd: Path, timeout: int = GIT_TIMEOUT
) -> tuple[int, str, str]:
	"""执行 git 命令，返回 (退出码, stdout, stderr)。"""
	env = dict(os.environ)
	env['GIT_TERMINAL_PROMPT'] = '0'
	try:
		completed = subprocess.run(
			['git', *args],
			cwd=str(cwd),
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			check=False,
			timeout=timeout,
			env=env,
		)
	except FileNotFoundError:
		return 1, '', '运行环境缺少 `git` 命令'
	except subprocess.TimeoutExpired:
		return 1, '', f'git {args[0]} 超时（{timeout} 秒）'
	except OSError as exc:
		return 1, '', str(exc)

	return (
		completed.returncode,
		completed.stdout.strip(),
		base.mask_secrets(completed.stderr.strip()),
	)


def current_branch(repo_root: Path) -> str:
	code, out, _ = _git(['rev-parse', '--abbrev-ref', 'HEAD'], repo_root)
	if code == 0 and out and out != 'HEAD':
		return out
	# 兜底：从环境变量里取（GitHub Actions 提供分支名）
	return os.environ.get('GITHUB_REF_NAME') or 'main'


def commit_and_push(
	repo_root: Path,
	items_path: Path,
	record: dict[str, Any],
	message: str,
	branch: str,
) -> tuple[str, str]:
	"""合并记录 → 提交 → 推送，失败时基于远端最新内容重试。

	返回 ``(动作, 错误信息)``。
	"""
	_git(['config', 'user.name', 'github-actions[bot]'], repo_root)
	_git(
		[
			'config',
			'user.email',
			'41898282+github-actions[bot]@users.noreply.github.com',
		],
		repo_root,
	)

	rel_path = items_path.relative_to(repo_root).as_posix()
	action = 'unchanged'
	last_error = ''

	for attempt in range(1, PUSH_ATTEMPTS + 1):
		if attempt == 1:
			# 尽力先同步最新内容；失败不影响后续（可能只是浅克隆/离线）
			_git(['pull', '--rebase', '--autostash', 'origin', branch], repo_root)
		else:
			# 推送失败：丢弃本地这次提交，从远端最新内容重新合并
			code, _, err = _git(['fetch', 'origin', branch], repo_root)
			if code != 0:
				last_error = f'拉取远端失败: {err}'
				continue
			_git(['reset', '--hard', f'origin/{branch}'], repo_root)

		action, error = merge_record(items_path, record)
		if error:
			return '', error
		if action == 'unchanged':
			return action, ''

		code, _, err = _git(['add', rel_path], repo_root)
		if code != 0:
			return '', f'git add 失败: {err}'

		code, out, _ = _git(['status', '--porcelain', '--', rel_path], repo_root)
		if code == 0 and not out.strip():
			return 'unchanged', ''

		code, _, err = _git(['commit', '-m', message], repo_root)
		if code != 0:
			return '', f'git commit 失败: {err}'

		code, _, err = _git(['push', 'origin', f'HEAD:{branch}'], repo_root)
		if code == 0:
			return action, ''

		last_error = err
		log(f'第 {attempt} 次推送失败：{err}')

	return '', f'git push 失败（已重试 {PUSH_ATTEMPTS} 次）：{last_error}'


# --------------------------------------------------------------------------- #
# 评论渲染
# --------------------------------------------------------------------------- #
def render_report(
	*,
	issue_number: str,
	label_name: str,
	repo_url: str,
	record: dict[str, Any] | None,
	revision: str,
	action: str,
	error: str,
	items_file: str,
	close_on_success: bool = False,
) -> tuple[str, bool]:
	"""生成收录结果评论正文，返回 (正文, 是否成功)。"""
	ok = not error
	lines: list[str] = [APPROVE_COMMENT_MARKER]

	if ok and action == 'added':
		lines.append('## ✅ 组件已收录')
	elif ok and action == 'updated':
		lines.append('## ♻️ 组件信息已更新')
	elif ok:
		lines.append('## ✅ 组件已在收录列表中')
	else:
		lines.append('## ❌ 组件收录失败')

	lines.append('')

	if ok:
		lines.append(
			f'Issue 被打上 `{label_name}` 标签，机器人已把该组件写入 `{items_file}`。'
		)
	else:
		lines.append(
			'本次收录没有对仓库做出任何修改，请处理下面的问题后重新打标签触发。'
		)
	lines.append('')

	if repo_url:
		lines.append(f'- 组件仓库：{repo_url}')
	if issue_number:
		lines.append(f'- Issue：#{issue_number}')
	if revision:
		lines.append(f'- 收录版本：`{revision[:7]}`')
	lines.append(f'- 变更文件：`{items_file}`')
	lines.append('')

	if record:
		lines.append('### 收录内容')
		lines.append('')
		lines.append('| 字段 | 值 |')
		lines.append('| --- | --- |')
		for field in base.REQUIRED_FIELDS:
			value = str(record.get(field, '')).replace('|', '\\|').replace('\n', ' ')
			lines.append(f'| `{field}` | {base.truncate(value, 200)} |')
		for field in ('repo', 'issue', 'revision', 'added_at'):
			if field not in record:
				continue
			value = str(record[field]).replace('|', '\\|').replace('\n', ' ')
			lines.append(f'| `{field}` | {base.truncate(value, 200)} |')
		lines.append('')

	if error:
		lines.append('### 失败原因')
		lines.append('')
		lines.append(error)
		lines.append('')

	lines.append('---')
	lines.append('')
	if close_on_success:
		lines.append('🔒 收录已完成，本 Issue 将自动关闭。')
		lines.append('')
	lines.append(
		'由收录机器人生成，同一个 Issue 重复触发只会更新本评论，不会重复上架。'
	)

	body = '\n'.join(lines)
	if len(body) > base.MAX_COMMENT_LENGTH:
		body = (
			body[: base.MAX_COMMENT_LENGTH]
			+ '\n\n... (内容过长已截断，完整结果见 CI 日志)'
		)
	return body, ok


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_record(
	meta: dict[str, Any], repo_url: str, issue_number: str, revision: str
) -> dict[str, Any]:
	"""把 meta.json 与来源信息合成一条待收录记录。"""
	record: dict[str, Any] = {}
	for field in base.REQUIRED_FIELDS:
		record[field] = str(meta.get(field, '')).strip()
	# 保留 meta.json 中的其它自定义字段
	for key, value in meta.items():
		if key not in record and key not in SOURCE_FIELDS:
			record[key] = value

	record['repo'] = repo_url
	if issue_number.isdigit():
		record['issue'] = int(issue_number)
	record['revision'] = revision
	record['added_at'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
	return record


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(
		description='把被打上「审核通过」标签的组件收录进 items.json'
	)
	parser.add_argument(
		'--issue-number', default=os.environ.get('ISSUE_NUMBER', ''), help='Issue 编号'
	)
	parser.add_argument(
		'--label-name',
		default=os.environ.get('LABEL_NAME', '审核通过'),
		help='触发本次收录的标签名（仅用于评论展示）',
	)
	parser.add_argument(
		'--repo-url', default=os.environ.get('REPO_URL', ''), help='组件仓库地址'
	)
	parser.add_argument(
		'--issue-body-file', default='', help='包含 Issue 正文的文件，用于解析仓库地址'
	)
	parser.add_argument('--items', default='items.json', help='items.json 路径')
	parser.add_argument('--render-out', default='', help='把渲染好的评论正文写入该文件')
	parser.add_argument(
		'--publish-comment',
		action='store_true',
		help='通过 GitHub API 把结果发布到 Issue（已存在则编辑）',
	)
	parser.add_argument(
		'--dry-run', action='store_true', help='只做校验与合并，不提交、不推送'
	)
	parser.add_argument(
		'--close-issue',
		action='store_true',
		help='收录成功后自动关闭该 Issue（--dry-run 时不关闭）',
	)
	parser.add_argument(
		'--repo-dir',
		default='',
		help='直接使用本地已存在的组件目录，跳过克隆（本地调试用）',
	)
	parser.add_argument(
		'--keep', default='', help='把克隆的组件仓库保留到该目录，便于本地调试'
	)
	args = parser.parse_args(argv)

	issue_number = str(args.issue_number or '').strip()
	items_path = Path(args.items).resolve()

	# 1. 解析仓库地址
	issue_body = ''
	if args.issue_body_file:
		body_file = Path(args.issue_body_file)
		if body_file.is_file():
			issue_body = body_file.read_text(encoding='utf-8')

	repo_url = base.normalize_repo_url(args.repo_url)
	if not repo_url and issue_body:
		repo_url = base.parse_issue_form(issue_body)
		log(f'从 Issue 表单中解析到仓库地址: {repo_url or "(为空)"}')
	repo_url = base.normalize_repo_url(repo_url)

	record: dict[str, Any] | None = None
	revision = ''
	action = ''
	error = ''
	workdir: Path | None = None

	try:
		# 本地调试模式允许不提供仓库地址
		if not repo_url and not args.repo_dir:
			error = 'Issue 中没有填写可用的仓库地址。'
		else:
			# 2. 取到组件仓库内容
			if args.repo_dir:
				repo_dir = Path(args.repo_dir).resolve()
				if not repo_dir.is_dir():
					error = f'本地组件目录不存在：`{repo_dir}`'
				else:
					log(f'本地目录模式，跳过克隆：{repo_dir}')
					# 本地调试时用目录路径顶替仓库地址，便于评论展示
					repo_url = repo_url or repo_dir.as_uri()
			else:
				workdir = Path(tempfile.mkdtemp(prefix='shop-approve-'))
				repo_dir = Path(args.keep).resolve() if args.keep else workdir / 'item'
				clone_check = base.clone_repository(repo_url, repo_dir)
				if not clone_check.ok:
					error = clone_check.detail

			if not error:
				code, out, _ = _git(['rev-parse', 'HEAD'], repo_dir)
				revision = out if code == 0 else ''

				# 3. 校验 meta.json
				meta, meta_check = base.check_meta(repo_dir)
				if meta is None or not meta_check.ok:
					error = meta_check.detail or '无法读取 `meta.json`。'
				else:
					record = build_record(meta, repo_url, issue_number, revision)

					# 4. 写入 items.json 并提交推送
					if args.dry_run:
						preview_action, preview_error = merge_record(
							items_path, record, dry_run=True
						)
						action = preview_action
						error = preview_error
						log(f'[dry-run] 合并动作：{action or "(失败)"}')
					else:
						committed_action, commit_error = commit_and_push(
							repo_root=items_path.parent,
							items_path=items_path,
							record=record,
							message=(
								f'chore(shop): 收录组件 {record["id"]}'
								+ (f'（#{issue_number}）' if issue_number else '')
							),
							branch=current_branch(items_path.parent),
						)
						action = committed_action
						error = commit_error
	finally:
		if workdir is not None and not args.keep:
			shutil.rmtree(workdir, ignore_errors=True)

	passed = not error
	close_on_success = bool(args.close_issue and passed and not args.dry_run)

	# 5. 渲染并回写评论
	body, _ = render_report(
		issue_number=issue_number,
		label_name=args.label_name,
		repo_url=repo_url,
		record=record,
		revision=revision,
		action=action,
		error=error,
		items_file=items_path.name,
		close_on_success=close_on_success,
	)

	if args.render_out:
		out_path = Path(args.render_out)
		out_path.parent.mkdir(parents=True, exist_ok=True)
		out_path.write_text(body, encoding='utf-8')
		log(f'评论正文已写入 {out_path}')

	base.write_summary(body)

	if args.publish_comment:
		# 直接覆盖校验 CI 那条评论的正文，不新建评论
		publish_check = base.publish_comment(issue_number, body)
		log(f'[{"PASS" if publish_check.ok else "FAIL"}] {publish_check.name}')
		if not publish_check.ok:
			log(publish_check.detail)
			base.write_summary(f'\n> 评论发布失败：{publish_check.detail}')

	# 6. 收录成功后才关闭 Issue（先发评论再关闭，保证用户先看到结果）
	if close_on_success:
		close_error = base.close_issue(issue_number)
		if close_error:
			log(f'[FAIL] 自动关闭 Issue：{close_error}')
			base.write_summary(f'\n> 自动关闭 Issue 失败：{close_error}')
		else:
			log(f'[PASS] 已自动关闭 Issue #{issue_number}')

	log(f'[{"PASS" if passed else "FAIL"}] 组件收录（{action or "无变更"}）')
	if error:
		log(error)

	return 0 if passed else 1


if __name__ == '__main__':
	sys.exit(main())
