#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布组件的自动化校验脚本。

用法::

    python3 check_publish.py --repo-url <组件仓库地址> [--items items.json] [--render-out 评论.md]

校验内容:
    1. meta.json 是否存在且必填字段完整（id/name/desc/author/enter）；
    2. id 是否与 components/items.json 中已收录的组件重复；
    3. meta.enter 指向的脚本能否被正常导入，且 ``import`` 期间没有异常。

所有结果都会写入 GitHub Actions 的 job summary；带 ``--render-out`` 时
额外输出一份可直接作为 Issue 评论使用的 Markdown 文件。

带 ``--publish-comment`` 时脚本会调用 GitHub API：若该 Issue 下已经存在
机器人发布的评论（通过隐藏标记识别），则直接**编辑**那条评论；否则才新建。
这样可以保证同一个 Issue 始终只有一条检查结果，不会刷屏。

脚本退出码: 0 表示全部通过，1 表示存在校验失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# meta.json 中的必填字段
REQUIRED_FIELDS: tuple[str, ...] = ('id', 'name', 'desc', 'author', 'enter')

# 单个字段允许的最大长度，避免超长内容刷屏 / 破坏渲染
MAX_FIELD_LENGTH = 2000

# id 只允许字母、数字、下划线、中划线、点
ID_PATTERN = re.compile(r'^[A-Za-z0-9_.-]+$')

# 导入组件脚本的默认超时时间（秒）
IMPORT_TIMEOUT = 60

# 评论正文最大长度（GitHub 评论上限 65536 字符，留出余量）
MAX_COMMENT_LENGTH = 60000

# 日志展示的最大字符数
MAX_LOG_CHARS = 3500

# 用于在 Issue 评论中识别「本机器人发布的评论」的隐藏标记
COMMENT_MARKER = '<!-- shop-publish-bot -->'

# Issue 表单中「运行自动检测」复选框的两种文案：
# 空闲态 = 等待用户勾选；运行态 = 本次检查正在进行
RUN_CHECKBOX_IDLE = '点我(重新)运行自动化测试'
RUN_CHECKBOX_RUNNING = '自动化测试正在运行'

# 匹配复选框整行，捕获：前缀 `- [`、方括号内的标记、`]` 之后到文案之前的部分、文案
_CHECKBOX_LINE_RE = re.compile(
	r'^([ \t]*-[ \t]*\[)([ xX])(\][ \t]*)('
	+ re.escape(RUN_CHECKBOX_IDLE)
	+ '|'
	+ re.escape(RUN_CHECKBOX_RUNNING)
	+ r')[ \t]*$',
	re.MULTILINE,
)


@dataclass
class Check:
	"""单条校验结果。

	ok=True 表示通过；ok=False 且 skipped=False 表示失败；
	ok=False 且 skipped=True 表示因前置条件不满足而跳过。
	"""

	name: str
	ok: bool
	detail: str = ''
	skipped: bool = False

	@property
	def status_icon(self) -> str:
		if self.skipped:
			return '⏭️'
		return '✅' if self.ok else '❌'

	@property
	def status_text(self) -> str:
		if self.skipped:
			return '跳过'
		return '通过' if self.ok else '失败'


@dataclass
class Context:
	"""一次运行所需的上下文信息。"""

	repo_url: str
	issue_number: str
	repo_dir: Path
	meta: dict[str, Any] | None
	checks: list[Check]


def log(message: str) -> None:
	"""输出到 stderr，避免污染 stdout。"""
	print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def truncate(text: str, limit: int = MAX_LOG_CHARS) -> str:
	"""截断过长文本并给出提示，防止评论体过大。"""
	text = text.strip()
	if len(text) <= limit:
		return text
	return f'{text[:limit]}\n... (已截断，完整内容见 CI 日志)'


def mask_secrets(text: str) -> str:
	"""清理日志中可能出现的令牌，避免泄露到公开 Issue。"""
	for env_name in ('GITHUB_TOKEN', 'GH_TOKEN', 'ACTIONS_TOKEN'):
		token = os.environ.get(env_name)
		if token:
			text = text.replace(token, '***')
	text = re.sub(r'(x-access-token:)[^@\s]+', r'\1***', text)
	text = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1***:***@', text)
	return text


def normalize_repo_url(value: str) -> str:
	"""把各种写法的仓库地址规范成可 clone 的 https 地址。"""
	url = (value or '').strip()
	if not url:
		return ''

	# 取第一行，避免用户把说明文字一起粘进输入框
	url = url.splitlines()[0].strip()

	if url.startswith('git@github.com:'):
		url = 'https://github.com/' + url[len('git@github.com:') :]

	if url.startswith('github.com/'):
		url = 'https://' + url

	# ssh 形式 https -> git
	url = re.sub(r'^ssh://git@github\.com/', 'https://github.com/', url)

	if url.endswith('.git'):
		url = url[: -len('.git')]

	url = url.rstrip('/')
	return url


def load_json(path: Path) -> Any:
	with path.open('r', encoding='utf-8') as handle:
		return json.load(handle)


def parse_issue_form(body: str, field_label: str = '仓库连接') -> str:
	"""从 Issue Form 渲染出的 Markdown 中提取指定字段的值。

	Issue Form 提交后正文形如::

		### 仓库连接

		https://github.com/owner/repo

		### 下一步
	"""
	if not body:
		return ''

	pattern = re.compile(
		r'^#{2,3}\s*' + re.escape(field_label) + r'\s*$(.*?)(?=^#{2,3}\s|\Z)',
		re.MULTILINE | re.DOTALL,
	)
	match = pattern.search(body)
	if not match:
		return ''

	value = match.group(1).strip()
	value = re.sub(r'^```[a-zA-Z]*\s*', '', value)  # 去掉代码块开头
	value = re.sub(r'\s*```$', '', value)  # 去掉代码块结尾
	value = re.sub(r'<!--.*?-->', '', value, flags=re.DOTALL).strip()

	for line in value.splitlines():
		line = line.strip()
		if line and line.lower() != '_no response_':
			return line
	return ''


def is_run_requested(body: str) -> bool:
	"""判断 Issue 正文里「点我(重新)运行自动化测试」是否被勾选。"""
	pattern = (
		r'^[ \t]*-[ \t]*\[[xX]\][ \t]*' + re.escape(RUN_CHECKBOX_IDLE) + r'[ \t]*$'
	)
	return re.search(pattern, body or '', re.MULTILINE) is not None


def set_run_checkbox(body: str, running: bool) -> str:
	"""把复选框切换到运行态或空闲态。

	运行态：``- [X] 自动化测试正在运行``
	空闲态：``- [ ] 点我(重新)运行自动化测试``
	"""
	mark = 'x' if running else ' '
	text = RUN_CHECKBOX_RUNNING if running else RUN_CHECKBOX_IDLE

	def repl(match: re.Match[str]) -> str:
		return f'{match.group(1)}{mark}{match.group(3)}{text}'

	return _CHECKBOX_LINE_RE.sub(repl, body or '')


def find_meta(root: Path) -> Path | None:
	"""在仓库根目录及一层子目录中查找 meta.json。"""
	direct = root / 'meta.json'
	if direct.is_file():
		return direct

	for candidate in sorted(root.glob('*/meta.json')):
		return candidate

	return None


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #
def check_meta(root: Path) -> tuple[dict[str, Any] | None, Check]:
	"""检查 meta.json 是否存在且字段完整。"""
	meta_path = find_meta(root)
	if meta_path is None:
		return None, Check(
			'meta.json 完整性',
			False,
			'在仓库根目录（或其一级子目录）中没有找到 `meta.json`。',
		)

	try:
		raw = meta_path.read_text(encoding='utf-8')
	except OSError as exc:
		return None, Check('meta.json 完整性', False, f'无法读取 `meta.json`: {exc}')

	try:
		meta = json.loads(raw)
	except json.JSONDecodeError as exc:
		return None, Check(
			'meta.json 完整性',
			False,
			f'`meta.json` 不是合法的 JSON: `{exc.msg}` (第 {exc.lineno} 行第 {exc.colno} 列)',
		)

	if not isinstance(meta, dict):
		return None, Check(
			'meta.json 完整性', False, '`meta.json` 顶层必须是一个 JSON 对象。'
		)

	missing = [field for field in REQUIRED_FIELDS if field not in meta]
	empty = [
		field
		for field in REQUIRED_FIELDS
		if field in meta and not str(meta.get(field) or '').strip()
	]
	wrong_type = [
		field
		for field in REQUIRED_FIELDS
		if field in meta and not isinstance(meta[field], str)
	]
	too_long = [
		field
		for field in REQUIRED_FIELDS
		if field in meta
		and isinstance(meta[field], str)
		and len(meta[field]) > MAX_FIELD_LENGTH
	]
	bad_id = (
		isinstance(meta.get('id'), str)
		and bool(meta['id'].strip())
		and not ID_PATTERN.match(meta['id'].strip())
	)

	problems: list[str] = []
	if missing:
		problems.append('缺少必填字段: ' + ', '.join(f'`{field}`' for field in missing))
	if empty:
		problems.append('以下字段为空: ' + ', '.join(f'`{field}`' for field in empty))
	if wrong_type:
		problems.append(
			'以下字段必须是字符串: ' + ', '.join(f'`{field}`' for field in wrong_type)
		)
	if too_long:
		problems.append(
			'以下字段内容过长: '
			+ ', '.join(f'`{field}`' for field in too_long)
			+ f'（上限 {MAX_FIELD_LENGTH} 字符）'
		)
	if bad_id:
		problems.append(
			'`id` 只能包含字母、数字、下划线、中划线、点，且不能包含空格: '
			f'`{meta.get("id")}`'
		)

	if problems:
		return meta, Check(
			'meta.json 完整性',
			False,
			f'存在以下问题（文件 `{meta_path.relative_to(root).as_posix()}`）：\n\n'
			+ '\n'.join(f'- {item}' for item in problems),
		)

	summary = ', '.join(
		f'`{field}` = `{truncate(str(meta[field]), 60)}`' for field in REQUIRED_FIELDS
	)
	return meta, Check(
		'meta.json 完整性',
		True,
		f'文件 `{meta_path.relative_to(root).as_posix()}`，字段齐全：{summary}',
	)


def check_duplicate_id(meta: dict[str, Any] | None, items_path: Path) -> Check:
	"""检查 id 是否与 items.json 中已收录的组件重复。"""
	name = 'ID 唯一性'

	if meta is None or not str(meta.get('id') or '').strip():
		return Check(
			name, False, 'meta.json 不完整，已跳过 `id` 重复校验。', skipped=True
		)

	new_id = str(meta['id']).strip()

	if not items_path.is_file():
		return Check(name, True, f'没有找到 `{items_path.name}`，跳过重复校验。')

	try:
		items = load_json(items_path)
	except (OSError, json.JSONDecodeError) as exc:
		return Check(name, False, f'无法解析 `{items_path.name}`: {exc}')

	if not isinstance(items, dict):
		return Check(name, True, f'`{items_path.name}` 不是对象结构，跳过重复校验。')

	# 支持 {id: {...}} 与 {..., "items": [...]} 两种结构
	existing = items
	if isinstance(items.get('items'), dict):
		existing = items['items']

	if new_id not in existing:
		return Check(name, True, f'`id` = `{new_id}` 未被占用。')

	record = existing[new_id]
	owner = ''
	if isinstance(record, dict):
		owner = str(record.get('repo') or record.get('url') or '').strip()

	detail = f'`id` = `{new_id}` 已存在于 `{items_path.name}`'
	if owner:
		detail += f'（已由 {owner} 发布）'
	detail += '，请修改 `meta.json` 中的 `id` 后重新提交。'
	return Check(name, False, detail)


def _sanitize_pythonpath(value: str | None) -> str:
	"""剔除 PYTHONPATH 中不存在的目录，避免站点注入失败刷一堆告警。"""
	if not value:
		return ''
	entries = [
		entry for entry in value.split(os.pathsep) if entry and Path(entry).is_dir()
	]
	return os.pathsep.join(entries)


# 在子进程中执行组件的引导代码：既能拿到完整 traceback，又能隔离副作用
_ENTRY_BOOTSTRAP = (
	'import importlib.util, sys\n'
	'spec = importlib.util.spec_from_file_location("shop_item_entry", sys.argv[1])\n'
	'module = importlib.util.module_from_spec(spec)\n'
	'sys.modules["shop_item_entry"] = module\n'
	'spec.loader.exec_module(module)\n'
)


def check_enter(root: Path, meta: dict[str, Any] | None) -> Check:
	"""检查 meta.enter 指向的脚本能否被正常导入并执行。"""
	name = 'enter 脚本导入'

	if meta is None or not str(meta.get('enter') or '').strip():
		return Check(
			name,
			False,
			'meta.json 不完整，已跳过 `enter` 校验。',
			skipped=True,
		)

	entry = str(meta['enter']).strip()
	entry_path = (root / entry).resolve()

	# 防御: enter 不能指向仓库之外
	try:
		entry_path.relative_to(root.resolve())
	except ValueError:
		return Check(name, False, f'`enter` = `{entry}` 指向了仓库之外，已拒绝执行。')

	if not entry_path.is_file():
		return Check(name, False, f'`enter` = `{entry}` 指向的文件不存在。')

	if entry_path.suffix != '.py':
		return Check(
			name,
			False,
			f'`enter` = `{entry}` 不是 Python 脚本，本商店仅支持 `.py` 组件。',
		)

	env = dict(os.environ)
	sanitized = _sanitize_pythonpath(env.get('PYTHONPATH'))
	if sanitized:
		env['PYTHONPATH'] = sanitized
	else:
		env.pop('PYTHONPATH', None)
	env['PYTHONIOENCODING'] = 'utf-8'
	env['PYTHONDONTWRITEBYTECODE'] = '1'
	env['PYTHONUNBUFFERED'] = '1'

	# 以组件仓库根目录为 cwd，与用户直接 `python3 <enter>` 的用法一致
	command = [sys.executable, '-c', _ENTRY_BOOTSTRAP, str(entry_path)]
	try:
		completed = subprocess.run(
			command,
			cwd=str(root),
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			env=env,
			timeout=IMPORT_TIMEOUT,
			check=False,
		)
	except subprocess.TimeoutExpired:
		return Check(
			name,
			False,
			f'导入 `{entry}` 超时（超过 {IMPORT_TIMEOUT} 秒）。组件在导入阶段不应执行阻塞操作。',
		)
	except OSError as exc:
		return Check(name, False, f'无法启动 Python 解释器：{exc}')

	stdout = mask_secrets(completed.stdout.decode('utf-8', 'replace'))
	stderr = mask_secrets(completed.stderr.decode('utf-8', 'replace'))

	if completed.returncode != 0:
		error_text = (stderr or stdout).strip() or '（没有输出任何错误信息）'
		detail = f'导入 `{entry}` 失败，退出码 `{completed.returncode}`：\n\n```\n{truncate(error_text)}\n```'
		if stdout.strip() and stderr.strip():
			detail += f'\n\n<details><summary>标准输出</summary>\n\n```\n{truncate(stdout)}\n```\n\n</details>'
		return Check(name, False, detail)

	detail = f'`{entry}` 导入成功，未发现异常。'
	if stdout.strip():
		detail += f'\n\n<details><summary>运行输出</summary>\n\n```\n{truncate(stdout)}\n```\n\n</details>'
	return Check(name, True, detail)


# --------------------------------------------------------------------------- #
# 评论渲染
# --------------------------------------------------------------------------- #
def render_comment(
	ctx: Context, run_url: str = '', revision: str = ''
) -> tuple[str, bool]:
	"""生成 Markdown 评论正文，返回 (正文, 是否全部通过)。"""
	# 跳过项不算失败，也不算通过
	passed = all(check.ok or check.skipped for check in ctx.checks)
	passed_count = sum(1 for check in ctx.checks if check.ok)
	effective_count = sum(1 for check in ctx.checks if not check.skipped)
	skipped_count = len(ctx.checks) - effective_count

	if passed and skipped_count:
		title = '## ⚠️ 组件发布检查通过（部分跳过）'
	elif passed:
		title = '## ✅ 组件发布检查通过'
	else:
		title = '## ❌ 组件发布检查未通过'

	# 隐藏标记用于后续运行定位「本机器人的评论」
	lines: list[str] = [COMMENT_MARKER, title, '']

	if passed:
		lines.append('所有检查项均已完成，组件可以进入人工审核流程。')
	else:
		lines.append(
			'请根据下方提示修改仓库内容后，**勾选 Issue 中的「重新运行自动化测试」**，机器人会直接更新本评论。'
		)
	lines.append('')

	# 概览表格
	lines.append('| 检查项 | 结果 |')
	lines.append('| --- | --- |')
	for check in ctx.checks:
		lines.append(f'| {check.name} | {check.status_icon} {check.status_text} |')
	lines.append('')
	summary_line = f'通过 **{passed_count}/{effective_count}** 项。'
	if skipped_count:
		summary_line += f' 另有 {skipped_count} 项因前置条件不满足而跳过。'
	lines.append(summary_line)
	lines.append('')

	# 明细
	lines.append('### 检查明细')
	lines.append('')
	for check in ctx.checks:
		# 只有「失败」的明细默认展开，跳过项保持折叠以免刷屏
		is_failure = not check.ok and not check.skipped
		lines.append(f'<details{" open" if is_failure else ""}>')
		lines.append(f'<summary>{check.status_icon} {check.name}</summary>')
		lines.append('')
		lines.append(check.detail or '_无附加信息_')
		lines.append('')
		lines.append('</details>')
		lines.append('')

	# 组件信息
	if ctx.meta:
		lines.append('### 组件信息')
		lines.append('')
		lines.append('| 字段 | 值 |')
		lines.append('| --- | --- |')
		for field in REQUIRED_FIELDS:
			value = str(ctx.meta.get(field, '')).replace('|', '\\|').replace('\n', ' ')
			lines.append(f'| `{field}` | {truncate(value, 200)} |')
		lines.append('')

	# 页脚
	lines.append('---')
	lines.append('')
	footer: list[str] = []
	if ctx.repo_url:
		footer.append(f'组件仓库：{ctx.repo_url}')
	if ctx.issue_number:
		footer.append(f'Issue：#{ctx.issue_number}')
	if revision:
		footer.append(f'校验版本：`{revision}`')
	footer.append('由发布机器人生成，重复运行会更新本评论而不是新发一条。')
	lines.extend(footer)

	body = '\n'.join(lines)
	if len(body) > MAX_COMMENT_LENGTH:
		body = (
			body[:MAX_COMMENT_LENGTH] + '\n\n... (内容过长已截断，完整结果见 CI 日志)'
		)
	return body, passed


def _api_request(
	method: str, url: str, token: str, payload: dict[str, Any] | None = None
) -> Any:
	"""调用 GitHub REST API。"""
	data = json.dumps(payload).encode('utf-8') if payload is not None else None
	request = urllib.request.Request(url, data=data, method=method)
	request.add_header('Accept', 'application/vnd.github+json')
	request.add_header('Authorization', f'Bearer {token}')
	request.add_header('X-GitHub-Api-Version', '2022-11-28')
	request.add_header('User-Agent', 'shop-publish-bot')
	if data is not None:
		request.add_header('Content-Type', 'application/json')

	with urllib.request.urlopen(request, timeout=60) as response:
		raw = response.read().decode('utf-8')
	return json.loads(raw) if raw.strip() else None


def publish_comment(issue_number: str, body: str) -> Check:
	"""把检查结果发布到 Issue：已存在则编辑，否则新建。"""
	name = '发布检查结果'

	token = os.environ.get('SHOP_BOT_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
	repo_slug = os.environ.get('GITHUB_REPOSITORY') or ''
	api_url = (os.environ.get('GITHUB_API_URL') or 'https://api.github.com').rstrip('/')

	if not issue_number or not str(issue_number).isdigit():
		return Check(name, False, '缺少有效的 Issue 编号，跳过评论发布。')
	if not repo_slug or '/' not in repo_slug:
		return Check(name, False, '缺少 `GITHUB_REPOSITORY` 环境变量，跳过评论发布。')
	if not token:
		return Check(name, False, '缺少可用的 Token，跳过评论发布。')

	issue = str(issue_number)
	base = f'{api_url}/repos/{repo_slug}/issues/{issue}/comments'

	# 先找出本机器人此前发布的评论
	existing_comment_id: int | None = None
	page = 1
	try:
		while True:
			comments = (
				_api_request('GET', f'{base}?per_page=100&page={page}', token) or []
			)
			if not comments:
				break
			for comment in comments:
				content = comment.get('body') or ''
				if COMMENT_MARKER in content:
					existing_comment_id = int(comment.get('id'))
					break
			if existing_comment_id is not None or len(comments) < 100:
				break
			page += 1
			if page > 20:  # 防御：最多翻 2000 条
				break
	except urllib.error.HTTPError as exc:
		detail = exc.read().decode('utf-8', 'replace') if exc.fp else str(exc)
		return Check(
			name,
			False,
			f'读取已有评论失败（HTTP {exc.code}）：\n\n```\n{truncate(detail)}\n```',
		)
	except (urllib.error.URLError, OSError) as exc:
		return Check(name, False, f'无法连接 GitHub API：{exc}')

	payload = {'body': body}
	try:
		if existing_comment_id is not None:
			_api_request(
				'PATCH',
				f'{api_url}/repos/{repo_slug}/issues/comments/{existing_comment_id}',
				token,
				payload,
			)
			return Check(
				name,
				True,
				f'已更新此前发布的评论（comment id `{existing_comment_id}`）。',
			)

		_api_request('POST', base, token, payload)
		return Check(name, True, '已新建检查结果评论。')
	except urllib.error.HTTPError as exc:
		detail = exc.read().decode('utf-8', 'replace') if exc.fp else str(exc)
		return Check(
			name,
			False,
			f'写入评论失败（HTTP {exc.code}）：\n\n```\n{truncate(detail)}\n```',
		)
	except (urllib.error.URLError, OSError) as exc:
		return Check(name, False, f'无法连接 GitHub API：{exc}')


def _github_api_context() -> tuple[str, str, str]:
	"""返回 (token, repo_slug, api_url)。"""
	token = os.environ.get('SHOP_BOT_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
	repo_slug = os.environ.get('GITHUB_REPOSITORY') or ''
	api_url = (os.environ.get('GITHUB_API_URL') or 'https://api.github.com').rstrip('/')
	return token, repo_slug, api_url


def fetch_issue_body(issue_number: str) -> tuple[str | None, str]:
	"""读取 Issue 当前正文，返回 (正文, 错误信息)。"""
	token, repo_slug, api_url = _github_api_context()
	if not token or not repo_slug:
		return None, '缺少 Token 或 GITHUB_REPOSITORY'

	url = f'{api_url}/repos/{repo_slug}/issues/{issue_number}'
	try:
		data = _api_request('GET', url, token) or {}
	except urllib.error.HTTPError as exc:
		detail = exc.read().decode('utf-8', 'replace') if exc.fp else str(exc)
		return None, f'读取 Issue 失败（HTTP {exc.code}）：{truncate(detail, 500)}'
	except (urllib.error.URLError, OSError) as exc:
		return None, f'读取 Issue 失败：{exc}'

	return str(data.get('body') or ''), ''


def update_issue_body(issue_number: str, body: str, expected: str | None = None) -> str:
	"""更新 Issue 正文，返回错误信息（空串表示成功）。

	``expected`` 用于乐观锁：仅当远端正文与预期一致时才写入，
	避免覆盖用户在 CI 运行期间手动做出的编辑。
	"""
	token, repo_slug, api_url = _github_api_context()
	if not token or not repo_slug:
		return '缺少 Token 或 GITHUB_REPOSITORY'

	if expected is not None:
		current, error = fetch_issue_body(issue_number)
		if error:
			return error
		if current != expected:
			return 'Issue 正文在运行期间被修改，已放弃本次状态回写'

	url = f'{api_url}/repos/{repo_slug}/issues/{issue_number}'
	try:
		_api_request('PATCH', url, token, {'body': body})
	except urllib.error.HTTPError as exc:
		detail = exc.read().decode('utf-8', 'replace') if exc.fp else str(exc)
		return f'更新 Issue 正文失败（HTTP {exc.code}）：{truncate(detail, 500)}'
	except (urllib.error.URLError, OSError) as exc:
		return f'更新 Issue 正文失败：{exc}'

	return ''


def write_summary(body: str) -> None:
	"""把结果写入 GitHub Actions job summary。"""
	summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
	if not summary_path:
		return
	try:
		with open(summary_path, 'a', encoding='utf-8') as handle:
			handle.write(body + '\n')
	except OSError as exc:  # pragma: no cover - 仅 CI 环境
		log(f'写入 job summary 失败: {exc}')


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def clone_repository(repo_url: str, target: Path) -> Check:
	"""浅克隆组件仓库，返回克隆结果。"""
	name = '拉取组件仓库'
	if not repo_url:
		return Check(name, False, 'Issue 中没有填写可用的仓库地址。')

	token = os.environ.get('SHOP_BOT_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
	auth_url = repo_url
	if token and repo_url.startswith('https://github.com/'):
		auth_url = repo_url.replace(
			'https://github.com/', f'https://x-access-token:{token}@github.com/'
		)

	env = dict(os.environ)
	sanitized = _sanitize_pythonpath(env.get('PYTHONPATH'))
	if sanitized:
		env['PYTHONPATH'] = sanitized
	else:
		env.pop('PYTHONPATH', None)
	env['GIT_TERMINAL_PROMPT'] = '0'

	command = ['git', 'clone', '--depth', '1', '--quiet', auth_url, str(target)]
	try:
		completed = subprocess.run(
			command,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			timeout=300,
			env=env,
			check=False,
		)
	except FileNotFoundError:
		return Check(name, False, '运行环境缺少 `git` 命令。')
	except subprocess.TimeoutExpired:
		return Check(name, False, '克隆仓库超时（300 秒）。')

	if completed.returncode != 0:
		error = mask_secrets(completed.stderr.decode('utf-8', 'replace'))
		return Check(
			name,
			False,
			f'无法克隆 `{repo_url}`，请确认仓库是**公开仓库**且地址正确。\n\n```\n{truncate(error)}\n```',
		)

	revision = ''
	try:
		revision = subprocess.run(
			['git', '-C', str(target), 'rev-parse', 'HEAD'],
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			check=False,
			text=True,
		).stdout.strip()
	except OSError:
		pass

	return Check(
		name,
		True,
		f'已克隆 `{repo_url}`' + (f'，commit `{revision[:7]}`' if revision else ''),
	)


def run_checks(repo_url: str, root: Path, items_path: Path) -> tuple[Context, str]:
	"""执行全部检查并返回上下文与版本号。"""
	checks: list[Check] = []

	meta, meta_check = check_meta(root)
	checks.append(meta_check)
	checks.append(check_duplicate_id(meta, items_path))
	checks.append(check_enter(root, meta))

	revision = ''
	try:
		revision = subprocess.run(
			['git', '-C', str(root), 'rev-parse', 'HEAD'],
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			check=False,
			text=True,
		).stdout.strip()
	except OSError:
		pass

	ctx = Context(
		repo_url=repo_url, issue_number='', repo_dir=root, meta=meta, checks=checks
	)
	return ctx, revision


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description='校验组件发布 Issue 中的仓库内容')
	parser.add_argument(
		'--repo-url', default=os.environ.get('REPO_URL', ''), help='组件仓库地址'
	)
	parser.add_argument(
		'--items',
		default=os.environ.get('ITEMS_PATH', 'items.json'),
		help='商店的 components/items.json 路径',
	)
	parser.add_argument(
		'--issue-number', default=os.environ.get('ISSUE_NUMBER', ''), help='Issue 编号'
	)
	parser.add_argument('--render-out', default='', help='把渲染好的评论正文写入该文件')
	parser.add_argument(
		'--publish-comment',
		action='store_true',
		help='通过 GitHub API 把结果发布到 Issue（已存在则编辑）',
	)
	parser.add_argument(
		'--keep', default='', help='把克隆的仓库保留到该目录，便于本地调试'
	)
	parser.add_argument(
		'--repo-dir',
		default='',
		help='直接校验本地已存在的组件目录，跳过克隆（本地调试用）',
	)
	parser.add_argument(
		'--issue-body-file',
		default='',
		help='包含 Issue 正文的文件，用于自动解析仓库地址',
	)
	parser.add_argument(
		'--require-checkbox',
		action='store_true',
		help='仅当 Issue 中「点我(重新)运行自动化测试」被勾选时才继续',
	)
	parser.add_argument(
		'--issue-state',
		action='store_true',
		help='在检查期间把复选框改为运行态，结束后恢复为空闲态',
	)
	args = parser.parse_args(argv)

	issue_body = ''
	if args.issue_body_file:
		body_file = Path(args.issue_body_file)
		if body_file.is_file():
			issue_body = body_file.read_text(encoding='utf-8')

	# 只在用户主动勾选时才执行检查，避免改标题/改正文意外触发
	if args.require_checkbox:
		if not is_run_requested(issue_body):
			log('复选框未勾选，跳过本次检查。')
			return 0
		log('检测到「运行自动化测试」已勾选，开始检查。')

	# 进入运行态：把「点我(重新)运行自动化测试」替换为「自动化测试正在运行」
	running_body: str | None = None
	if args.issue_state and args.issue_number:
		running_body = set_run_checkbox(issue_body, running=True)
		if running_body != issue_body:
			error = update_issue_body(
				args.issue_number, running_body, expected=issue_body
			)
			if error:
				log(f'标记运行态失败: {error}')
				running_body = None
			else:
				log('已把复选框标记为「自动化测试正在运行」。')

	repo_url = args.repo_url
	if not normalize_repo_url(repo_url) and issue_body:
		repo_url = parse_issue_form(issue_body)
		log(f'从 Issue 表单中解析到仓库地址: {repo_url or "(为空)"}')

	repo_url = normalize_repo_url(repo_url)
	items_path = Path(args.items).resolve()

	if args.repo_dir:
		# 本地调试：直接对已有目录做检查，不访问网络
		repo_dir = Path(args.repo_dir).resolve()
		if not repo_dir.is_dir():
			log(f'本地组件目录不存在: {repo_dir}')
			return 2
		workdir = None
		ctx, revision = run_checks(repo_url, repo_dir, items_path)
		ctx.checks.insert(
			0, Check('拉取组件仓库', True, f'本地目录模式，跳过克隆：`{repo_dir}`')
		)
	else:
		workdir = Path(tempfile.mkdtemp(prefix='shop-publish-'))
		repo_dir = Path(args.keep).resolve() if args.keep else workdir / 'repo'

		clone_check = clone_repository(repo_url, repo_dir)

		if clone_check.ok:
			ctx, revision = run_checks(repo_url, repo_dir, items_path)
			ctx.checks.insert(0, clone_check)
		else:
			ctx = Context(
				repo_url=repo_url,
				issue_number='',
				repo_dir=repo_dir,
				meta=None,
				checks=[clone_check],
			)
			revision = ''

	ctx.issue_number = args.issue_number
	run_url = ''
	server = os.environ.get('GITHUB_SERVER_URL')
	repo_slug = os.environ.get('GITHUB_REPOSITORY')
	run_id = os.environ.get('GITHUB_RUN_ID')
	if server and repo_slug and run_id:
		run_url = f'{server}/{repo_slug}/actions/runs/{run_id}'

	body, passed = render_comment(ctx, run_url=run_url, revision=revision)

	if args.render_out:
		out_path = Path(args.render_out)
		out_path.parent.mkdir(parents=True, exist_ok=True)
		out_path.write_text(body, encoding='utf-8')
		log(f'评论正文已写入 {out_path}')

	write_summary(body)

	if args.publish_comment:
		# 发布结果不计入评论正文，避免「评论里包含自己的状态」这类循环
		publish_check = publish_comment(ctx.issue_number, body)
		log(f'[{"PASS" if publish_check.ok else "FAIL"}] {publish_check.name}')
		if not publish_check.ok:
			log(publish_check.detail)
			write_summary(f'\n> 评论发布失败：{publish_check.detail}')

	# 清理临时目录
	if workdir is not None and not args.keep:
		shutil.rmtree(workdir, ignore_errors=True)

	for check in ctx.checks:
		log(f'[{"PASS" if check.ok else "FAIL"}] {check.name}')

	# 退出空闲态：去掉勾选、恢复原文案，供下次重新触发。
	# 放在 main 末尾，保证前面各分支正常走完时一定恢复。
	if running_body is not None:
		idle_body = set_run_checkbox(running_body, running=False)
		error = update_issue_body(args.issue_number, idle_body, expected=running_body)
		if error:
			log(f'恢复复选框失败: {error}')
		else:
			log('已恢复复选框为空闲态。')

	return 0 if passed else 1


def _force_idle_state() -> None:
	"""兜底把 Issue 复选框恢复为空闲态，避免一直卡在「正在运行」。"""
	issue_number = os.environ.get('ISSUE_NUMBER', '')
	if not issue_number:
		return
	current, error = fetch_issue_body(issue_number)
	if error or current is None:
		log(f'兜底恢复跳过：{error}')
		return
	idle = set_run_checkbox(current, running=False)
	if idle == current:
		return
	error = update_issue_body(issue_number, idle, expected=current)
	if error:
		log(f'兜底恢复失败：{error}')
	else:
		log('兜底：已恢复复选框为空闲态。')


if __name__ == '__main__':
	# 兜底：任何未预期异常也要尽量把错误写回 Issue，而不是静默失败
	try:
		sys.exit(main())
	except Exception as unexpected:  # noqa: BLE001
		log(f'检查脚本内部错误: {unexpected!r}')
		_force_idle_state()
		body = '\n'.join(
			[
				COMMENT_MARKER,
				'## ⚠️ 组件发布检查异常中断',
				'',
				'自动化检查脚本自身出现了错误，请等待维护者查看 CI 日志。',
				'',
				'```',
				truncate(f'{type(unexpected).__name__}: {unexpected}'),
				'```',
			]
		)
		try:
			publish_comment(os.environ.get('ISSUE_NUMBER', ''), body)
		except Exception as publish_error:  # noqa: BLE001
			log(f'回写 Issue 失败: {publish_error!r}')
		sys.exit(1)
