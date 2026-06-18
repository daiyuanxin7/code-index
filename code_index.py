#!/usr/bin/env python3
"""
code-index —— 通用代码索引工具

把本文件放到项目根目录，运行 `python3 code_index.py --build` 即可自动检测项目类型
并构建代码索引，之后用一条命令快速定位接口 / 方法 / Mapper / 前端路由的代码位置。

专为「AI 助手 + 人」快速定位代码而设计，目标项目类型：
  - Java / Spring Boot / MyBatis 后端（Controller 接口、Java 方法、Mapper + XML SQL）
  - RuoYi 风格 Vue 前端（数据库 sys_menu 动态路由，或 src/router 静态路由）

────────────────────────────────────────────────────────────
快速上手
────────────────────────────────────────────────────────────
  构建索引（自动检测项目类型）:   python3 code_index.py --build
  查看检测结果 / 配置状态:        python3 code_index.py --doctor

  查接口 / 前端路由（智能匹配）:  python3 code_index.py /sys/user/list
  查接口（模糊）:                 python3 code_index.py userList
  查 Java 方法:                   python3 code_index.py --method selectUserById
  查 Mapper（接口 + XML SQL）:    python3 code_index.py --mapper selectById
  查前端路由:                     python3 code_index.py --route p_user_0001
  列出全部接口 / 路由:            python3 code_index.py --list [api|route]

  指定项目根目录:                 python3 code_index.py --build --project /path/to/project

详细说明见 README.md。
"""

from __future__ import annotations  # 让 `str | None` 注解在 Python 3.8+ 也能解析

import os
import re
import json
import sys
import time
import argparse
import subprocess
import configparser

# ════════════════════════════════════════════════════════════════════════════
# 全局常量
# ════════════════════════════════════════════════════════════════════════════

# 遍历项目目录时跳过的目录（避免扫描依赖、产物、IDE 配置，既快又干净）
IGNORE_DIRS = frozenset({
    ".git", ".svn", ".hg",
    "node_modules", "bower_components",
    "target", "build", "dist", "out", "bin",
    ".idea", ".vscode", ".settings",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    "venv", ".venv", "env",
    ".code-index",            # 本工具自身的产物目录
})

INDEX_DIR_NAME = ".code-index"   # 索引文件统一存放目录（建议加入 .gitignore）
CONFIG_FILE_NAME = "code-index.ini"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 运行时追加的忽略目录（来自配置 [scan] ignore_dirs），由 main() 在加载配置后填充
EXTRA_IGNORE_DIRS: set = set()


def _walk(root):
    """os.walk 的封装，自动剪掉 IGNORE_DIRS（+ 配置追加的 EXTRA_IGNORE_DIRS），避免扫描 node_modules / target 等。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_DIRS and d not in EXTRA_IGNORE_DIRS]
        yield dirpath, dirnames, filenames


# ════════════════════════════════════════════════════════════════════════════
# 配置驱动：约定默认值（DEFAULTS）
# ────────────────────────────────────────────────────────────────────────────
# 把原本散落在各处的硬编码「项目约定」集中到这里，作为内置默认 = 现有行为。
# 用户可在 code-index.ini 的 [scan]/[java]/[mapper]/[vue] 段覆盖，从而适配
# 不同目录结构 / 命名约定 / 注解，而无需改动核心代码。
# ════════════════════════════════════════════════════════════════════════════

DEFAULTS = {
    "scan": {
        "ignore_dirs": [],                          # 追加忽略目录（并入内置 IGNORE_DIRS）
    },
    "java": {
        "source_marker": "src/main/java",           # 后端源码根标志（相对各模块）
        "source_suffixes": [".java"],               # 方法索引扫描的源文件后缀
        "controller_suffixes": ["Controller.java"], # API 索引：Controller 文件名后缀
        "class_mapping_annotation": "RequestMapping",   # 类级路径前缀注解
        # HTTP 映射注解 → HTTP 方法
        "mapping_annotations": {
            "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
            "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": "ANY",
        },
    },
    "mapper": {
        "java_suffixes": ["Mapper.java"],           # Mapper 接口文件后缀
        "xml_marker": "src/main/resources/mapper",  # Mapper XML 根标志（相对各模块）
        "xml_suffixes": [".xml"],                   # Mapper XML 文件后缀
        "sql_tags": ["select", "insert", "update", "delete"],   # XML SQL 标签
        "inline_sql_annotations": ["Select", "Insert", "Update", "Delete"],  # 内联 SQL 注解
    },
    "vue": {
        "root_markers": ["src/views", "src/router"],    # 前端根标志目录（任一存在即为前端根）
        "router_dir": "src/router",                 # 自动扫描的静态路由目录
        "views_alias": "@/views",                   # 视图组件别名前缀（path→文件 映射用）
    },
}


def _split_list(raw: str) -> list:
    """把逗号 / 换行分隔的字符串拆成去空白列表。"""
    return [s.strip() for s in re.split(r"[,\n]", raw or "") if s.strip()]


def _parse_ann_map(raw: str) -> dict:
    """把 'GetMapping:GET, RequestMapping:ANY' 解析成 {注解: HTTP方法}，缺方法默认 ANY。"""
    out = {}
    for item in _split_list(raw):
        if ":" in item:
            k, v = item.split(":", 1)
            out[k.strip()] = v.strip() or "ANY"
        else:
            out[item] = "ANY"
    return out


def _merge_conventions(parser: configparser.ConfigParser | None) -> dict:
    """以 DEFAULTS 为基底，用 ini 的 [scan]/[java]/[mapper]/[vue] 段覆盖，返回生效约定。"""
    import copy
    conv = copy.deepcopy(DEFAULTS)
    if parser is None:
        return conv

    def ov_list(sec, key):
        if parser.has_option(sec, key):
            conv[sec][key] = _split_list(parser.get(sec, key))

    def ov_str(sec, key):
        if parser.has_option(sec, key):
            val = parser.get(sec, key).strip()
            if val:
                conv[sec][key] = val

    ov_list("scan", "ignore_dirs")
    ov_str("java", "source_marker")
    ov_list("java", "source_suffixes")
    ov_list("java", "controller_suffixes")
    ov_str("java", "class_mapping_annotation")
    if parser.has_option("java", "mapping_annotations"):
        conv["java"]["mapping_annotations"] = _parse_ann_map(
            parser.get("java", "mapping_annotations"))
    ov_list("mapper", "java_suffixes")
    ov_str("mapper", "xml_marker")
    ov_list("mapper", "xml_suffixes")
    ov_list("mapper", "sql_tags")
    ov_list("mapper", "inline_sql_annotations")
    ov_list("vue", "root_markers")
    ov_str("vue", "router_dir")
    ov_str("vue", "views_alias")
    return conv


# ════════════════════════════════════════════════════════════════════════════
# 项目检测 + 上下文
# ════════════════════════════════════════════════════════════════════════════

def detect_java_roots(project_root: str, marker: str = "src/main/java") -> list[str]:
    """递归查找所有后端源码根（默认 src/main/java），兼容单/多模块 Maven 项目。"""
    parts = marker.strip("/").split("/")
    roots = []
    for dirpath, dirnames, _ in _walk(project_root):
        java_root = os.path.join(dirpath, *parts)
        if os.path.isdir(java_root):
            roots.append(java_root)
            dirnames[:] = []  # 命中后不再深入，避免嵌套模块重复计入
    return roots


def detect_mapper_xml_roots(project_root: str, marker: str = "src/main/resources/mapper") -> list[str]:
    """递归查找所有 Mapper XML 根目录（默认 src/main/resources/mapper）。"""
    parts = marker.strip("/").split("/")
    roots = []
    for dirpath, dirnames, _ in _walk(project_root):
        xml_root = os.path.join(dirpath, *parts)
        if os.path.isdir(xml_root):
            roots.append(xml_root)
            dirnames[:] = []
    return roots


def derive_mapper_java_roots(java_src_roots: list[str]) -> list[str]:
    """在每个 java 源码根下定位 mapper 目录；找不到则回退为整模块扫描。"""
    result = []
    for java_root in java_src_roots:
        found = False
        for root, dirs, _ in _walk(java_root):
            if os.path.basename(root) == "mapper":
                result.append(root)
                found = True
        if not found:
            result.append(java_root)
    return result


def detect_vue_roots(project_root: str, root_markers=("src/views", "src/router")) -> list[str]:
    """
    查找 Vue 前端项目根（含任一标志目录，默认 src/views 或 src/router）。
    返回去重后的前端根目录列表。
    """
    marker_parts = [m.strip("/").split("/") for m in root_markers]
    roots = []
    for dirpath, dirnames, _ in _walk(project_root):
        if any(os.path.isdir(os.path.join(dirpath, *parts)) for parts in marker_parts):
            roots.append(dirpath)
            dirnames[:] = []  # 命中前端根后不再深入
    return roots


class Project:
    """承载一次运行的项目上下文：根目录、各类源码根、索引文件路径、配置。"""

    def __init__(self, root: str, config: dict, *, scan: bool = True):
        self.root = os.path.abspath(root)
        self.config = config
        # 生效的项目约定（DEFAULTS + ini 覆盖）；缺省时回退内置默认
        self.conv = config.get("conv") or _merge_conventions(None)

        if scan:
            self.java_roots = detect_java_roots(self.root, self.conv["java"]["source_marker"])
            self.mapper_xml_roots = detect_mapper_xml_roots(self.root, self.conv["mapper"]["xml_marker"])
            self.mapper_java_roots = derive_mapper_java_roots(self.java_roots)
            self.vue_roots = detect_vue_roots(self.root, self.conv["vue"]["root_markers"])
        else:
            self.java_roots = []
            self.mapper_xml_roots = []
            self.mapper_java_roots = []
            self.vue_roots = []

        self.index_dir = os.path.join(self.root, INDEX_DIR_NAME)
        self.api_index_file = os.path.join(self.index_dir, "api_index.json")
        self.method_index_file = os.path.join(self.index_dir, "method_index.json")
        self.mapper_index_file = os.path.join(self.index_dir, "mapper_index.json")
        self.route_index_file = os.path.join(self.index_dir, "route_index.json")
        self.manifest_file = os.path.join(self.index_dir, "manifest.json")

    def has_java(self) -> bool:
        return bool(self.java_roots)

    def has_vue(self) -> bool:
        return bool(self.vue_roots)

    def ensure_index_dir(self):
        os.makedirs(self.index_dir, exist_ok=True)

    def rel(self, path: str) -> str:
        """把绝对路径转成相对项目根的展示路径。"""
        try:
            return os.path.relpath(path, self.root)
        except ValueError:
            return path


# ════════════════════════════════════════════════════════════════════════════
# 配置加载（凭据外置：CLI 参数 > 环境变量 > 配置文件 > 无默认）
# ════════════════════════════════════════════════════════════════════════════

def load_config(project_root: str, config_path: str | None) -> dict:
    """
    读取数据库等敏感配置。优先级：
        命令行参数（在 CLI 层覆盖） > 环境变量 > code-index.ini > 空
    本函数负责「环境变量 + 配置文件」两层，命令行覆盖在 build_route_index 里处理。
    绝不在源码中硬编码任何凭据。
    """
    cfg: dict = {"vue_route_db": {}, "route_files": []}

    # 1) 配置文件
    parser = None
    path = config_path or os.path.join(project_root, CONFIG_FILE_NAME)
    if os.path.isfile(path):
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except Exception as e:
            print(f"[配置] 读取 {path} 失败：{e}")
            parser = None
        if parser and parser.has_section("vue_route_db"):
            cfg["vue_route_db"] = dict(parser.items("vue_route_db"))
        # 手动指定的静态路由文件（逗号或换行分隔），用于路由不在 src/router 的项目
        if parser and parser.has_section("vue_route_static"):
            raw = parser.get("vue_route_static", "files", fallback="")
            cfg["route_files"] += [s.strip() for s in re.split(r"[,\n]", raw) if s.strip()]

    # 2) 环境变量覆盖
    env_map = {
        "host": "CODE_INDEX_DB_HOST",
        "port": "CODE_INDEX_DB_PORT",
        "database": "CODE_INDEX_DB_NAME",
        "user": "CODE_INDEX_DB_USER",
        "password": "CODE_INDEX_DB_PASSWORD",
        "table": "CODE_INDEX_DB_TABLE",
    }
    for key, env in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg["vue_route_db"][key] = val

    # 3) 环境变量指定的静态路由文件（逗号分隔）
    env_files = os.environ.get("CODE_INDEX_ROUTE_FILES")
    if env_files:
        cfg["route_files"] += [s.strip() for s in env_files.split(",") if s.strip()]

    # 4) 项目约定（DEFAULTS + ini 的 [scan]/[java]/[mapper]/[vue] 覆盖）
    cfg["conv"] = _merge_conventions(parser)

    return cfg


# ════════════════════════════════════════════════════════════════════════════
# 通用 Java 解析工具
# ════════════════════════════════════════════════════════════════════════════

_JAVA_KEYWORDS = frozenset([
    "if", "for", "while", "switch", "catch", "try", "new", "return",
    "class", "interface", "enum", "instanceof", "throw", "assert"
])


def _find_class_body_start(lines: list[str]) -> tuple[int, str]:
    total = len(lines)
    for idx, line in enumerate(lines):
        m = re.search(r'\b(?:class|interface|enum)\s+(\w+)', line)
        if m:
            class_name = m.group(1)
            j = idx
            while j < total and '{' not in lines[j]:
                j += 1
            return j + 1, class_name
    return 0, ""


def _count_braces(line: str) -> int:
    stripped = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '""', line)
    stripped = re.sub(r'//.*$', '', stripped)
    return stripped.count('{') - stripped.count('}')


def _find_method_end(lines: list[str], brace_open_idx: int) -> int:
    depth = 0
    for i in range(brace_open_idx, len(lines)):
        depth += _count_braces(lines[i])
        if depth <= 0:
            return i
    return len(lines) - 1


# ════════════════════════════════════════════════════════════════════════════
# Java：API 索引（Controller 中的 HTTP 映射注解）
# ════════════════════════════════════════════════════════════════════════════

MAPPING_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "ANY",
}

_PATH_RE = re.compile(r'"(/[^"]*)"')


def _extract_paths_from_annotation(text: str) -> list[str]:
    paths = _PATH_RE.findall(text)
    return paths if paths else [""]


def parse_controller(filepath: str, mapping_annotations: dict = MAPPING_ANNOTATIONS,
                     class_annotation: str = "RequestMapping") -> list[dict]:
    """解析单个 Controller，返回 API 接口列表（含类级前缀注解拼接）。
    mapping_annotations: {注解名: HTTP方法}；class_annotation: 类级路径前缀注解名。"""
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    total = len(lines)
    class_prefixes = [""]
    cls_ann = re.escape(class_annotation)

    class_body_start, _ = _find_class_body_start(lines)
    for idx, line in enumerate(lines):
        if idx >= class_body_start:
            break
        stripped = line.strip()
        m = re.match(rf'@{cls_ann}\s*\(([^)]*)\)', stripped)
        if m:
            class_prefixes = _extract_paths_from_annotation(m.group(1))
            continue
        m2 = re.match(rf'@{cls_ann}\s*\(\s*"([^"]*)"\s*\)', stripped)
        if m2:
            class_prefixes = [m2.group(1)]

    results = []
    annotation_name = None
    annotation_lines_buf = []
    annotation_start_line = 0
    i = class_body_start

    while i < total:
        line = lines[i]
        stripped = line.strip()

        if annotation_name is None:
            for ann in mapping_annotations:
                pattern = rf'^@{re.escape(ann)}\s*[\(\"(]'
                if re.match(pattern, stripped) or stripped == f'@{ann}':
                    annotation_name = ann
                    annotation_lines_buf = [stripped]
                    annotation_start_line = i + 1
                    break

        if annotation_name:
            full = "".join(annotation_lines_buf)
            open_count = full.count('(') - full.count(')')

            j = i + 1
            while open_count > 0 and j < total:
                nxt = lines[j].strip()
                annotation_lines_buf.append(nxt)
                open_count += nxt.count('(') - nxt.count(')')
                j += 1

            full_annotation = " ".join(annotation_lines_buf)

            inner = re.search(r'@\w+\s*\((.+)\)', full_annotation, re.DOTALL)
            if inner:
                method_paths = _extract_paths_from_annotation(inner.group(1))
            elif re.match(r'@\w+\s*$', full_annotation.strip()):
                method_paths = [""]
            else:
                direct = re.search(r'"(/[^"]*)"', full_annotation)
                method_paths = [direct.group(1)] if direct else [""]

            method_line_start = j
            while method_line_start < total:
                l = lines[method_line_start].strip()
                if l and not l.startswith('@') and not l.startswith('//') and not l.startswith('*'):
                    break
                method_line_start += 1

            brace_line = method_line_start
            while brace_line < total and '{' not in lines[brace_line]:
                brace_line += 1

            http_method = mapping_annotations[annotation_name]
            for prefix in class_prefixes:
                for mpath in method_paths:
                    full_path = (prefix.rstrip('/') + '/' + mpath.lstrip('/')).rstrip('/')
                    if not full_path:
                        full_path = '/'
                    results.append({
                        "path": full_path,
                        "method": http_method,
                        "file": filepath,
                        "line_start": annotation_start_line,
                        "line_end": brace_line + 1,
                    })

            annotation_name = None
            annotation_lines_buf = []
            i = brace_line + 1
            continue

        i += 1

    return results


def build_api_index(proj: Project) -> dict:
    jconf = proj.conv["java"]
    suffixes = tuple(jconf["controller_suffixes"])
    mapping_annotations = jconf["mapping_annotations"]
    class_annotation = jconf["class_mapping_annotation"]

    index: dict = {}
    controller_files = []
    for java_root in proj.java_roots:
        for root, _, files in _walk(java_root):
            for fname in files:
                if fname.endswith(suffixes):
                    controller_files.append(os.path.join(root, fname))

    print(f"[API] 找到 {len(controller_files)} 个 Controller 文件，正在解析...")
    for fpath in controller_files:
        try:
            for entry in parse_controller(fpath, mapping_annotations, class_annotation):
                index.setdefault(entry["path"], []).append(entry)
        except Exception as e:
            print(f"  [警告] {fpath}: {e}")

    sorted_index = dict(sorted(index.items()))
    proj.ensure_index_dir()
    with open(proj.api_index_file, "w", encoding="utf-8") as f:
        json.dump(sorted_index, f, ensure_ascii=False, indent=2)

    total_apis = sum(len(v) for v in sorted_index.values())
    print(f"[API] 完成：{total_apis} 个接口，{len(sorted_index)} 条路径 → {proj.rel(proj.api_index_file)}")
    return sorted_index


# ════════════════════════════════════════════════════════════════════════════
# Java：方法索引（所有 Java 文件中的方法定义）
# ════════════════════════════════════════════════════════════════════════════

_JAVA_MODIFIERS = (
    "public", "private", "protected", "static", "final", "abstract",
    "synchronized", "native", "default", "transactional", "override"
)
_MOD_GROUP = "(?:" + "|".join(_JAVA_MODIFIERS) + r")\s+"
METHOD_DEF_RE = re.compile(
    r'^\s*(?:@\w+(?:\([^)]*\))?\s+)*'
    r'(?:' + _MOD_GROUP + r')+'
    r'(?:<[^>]+>\s+)?'
    r'(?:[\w<>\[\],?.]+\s+)+'
    r'(\w+)\s*\('
)


def parse_java_methods(filepath: str) -> list[dict]:
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    total = len(lines)
    class_body_start, class_name = _find_class_body_start(lines)
    if class_body_start == 0:
        return []

    results = []
    brace_depth = 1
    i = class_body_start

    while i < total:
        line = lines[i]
        stripped = line.strip()

        if brace_depth == 1 and not stripped.startswith('//') \
                and not stripped.startswith('*') and not stripped.startswith('/*'):

            m = METHOD_DEF_RE.match(line)
            if m:
                method_name = m.group(1)
                if method_name not in _JAVA_KEYWORDS:
                    sig_start_line = i

                    sig_buf = []
                    k = i
                    while k < total:
                        sig_buf.append(lines[k].rstrip())
                        if '{' in lines[k] or (';' in lines[k] and '{' not in lines[k]):
                            break
                        k += 1

                    is_abstract = k < total and ';' in lines[k] and '{' not in lines[k]

                    if not is_abstract:
                        brace_open = k
                        method_end = _find_method_end(lines, brace_open)

                        signature = ' '.join(l.strip() for l in sig_buf[:2])
                        if '{' in signature:
                            signature = signature[:signature.index('{')].strip()

                        results.append({
                            "method": method_name,
                            "class": class_name,
                            "file": filepath,
                            "line_start": sig_start_line + 1,
                            "line_end": method_end + 1,
                            "signature": signature[:120],
                        })

                        i = method_end + 1
                        brace_depth = 1
                        continue
                    else:
                        signature = ' '.join(l.strip() for l in sig_buf)
                        results.append({
                            "method": method_name,
                            "class": class_name,
                            "file": filepath,
                            "line_start": sig_start_line + 1,
                            "line_end": k + 1,
                            "signature": signature[:120].rstrip(';').strip(),
                        })
                        i = k + 1
                        continue

        brace_depth += _count_braces(line)
        i += 1

    return results


def build_method_index(proj: Project) -> dict:
    suffixes = tuple(proj.conv["java"]["source_suffixes"])
    index: dict = {}
    java_files = []
    for java_root in proj.java_roots:
        for root, _, files in _walk(java_root):
            for fname in files:
                if fname.endswith(suffixes):
                    java_files.append(os.path.join(root, fname))

    print(f"[方法] 找到 {len(java_files)} 个 Java 文件，正在解析...")
    for fpath in java_files:
        try:
            for entry in parse_java_methods(fpath):
                index.setdefault(entry["method"], []).append(entry)
        except Exception as e:
            print(f"  [警告] {fpath}: {e}")

    sorted_index = dict(sorted(index.items()))
    proj.ensure_index_dir()
    with open(proj.method_index_file, "w", encoding="utf-8") as f:
        json.dump(sorted_index, f, ensure_ascii=False, indent=2)

    total_methods = sum(len(v) for v in sorted_index.values())
    print(f"[方法] 完成：{total_methods} 个方法定义，{len(sorted_index)} 个方法名 → {proj.rel(proj.method_index_file)}")
    return sorted_index


# ════════════════════════════════════════════════════════════════════════════
# Java：Mapper 索引（Mapper 接口 + XML SQL 关联）
# ════════════════════════════════════════════════════════════════════════════

_MAPPER_SQL_TAGS = ("select", "insert", "update", "delete")


def _collect_annotation_block(lines: list[str], start: int) -> tuple[int, str]:
    buf = lines[start]
    depth = buf.count('(') - buf.count(')')
    i = start + 1
    while depth > 0 and i < len(lines):
        buf += lines[i]
        depth += lines[i].count('(') - lines[i].count(')')
        i += 1
    return i, buf


def parse_mapper_java(filepath: str,
                      inline_annotations=("Select", "Insert", "Update", "Delete")) -> list[dict]:
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    inline_re = re.compile(r'@(' + '|'.join(re.escape(a) for a in inline_annotations) + r')\b')

    total = len(lines)
    class_body_start, class_name = _find_class_body_start(lines)
    results = []
    i = class_body_start

    while i < total:
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith('//') or stripped.startswith('*') \
                or stripped.startswith('/*'):
            i += 1
            continue

        if stripped.startswith('@'):
            inline_sql_type = None
            ann_start_line = i

            m_inline = inline_re.match(stripped)
            if m_inline:
                inline_sql_type = m_inline.group(1).lower()

            i, _ = _collect_annotation_block(lines, i)

            while i < total:
                ns = lines[i].strip()
                if ns.startswith('@'):
                    m2 = inline_re.match(ns)
                    if m2:
                        inline_sql_type = m2.group(1).lower()
                    i, _ = _collect_annotation_block(lines, i)
                elif not ns or ns.startswith('//') or ns.startswith('*'):
                    i += 1
                else:
                    break

            if i >= total:
                break

            method_decl_line = i
        else:
            inline_sql_type = None
            ann_start_line = i
            method_decl_line = i

        decl_line = lines[method_decl_line]
        first_paren = decl_line.find('(')
        if first_paren <= 0:
            i = method_decl_line + 1
            continue

        before_paren = decl_line[:first_paren]
        m_name = re.search(r'(\w+)\s*$', before_paren)
        if not m_name or m_name.group(1) in _JAVA_KEYWORDS:
            i = method_decl_line + 1
            continue

        method_name = m_name.group(1)

        sig_buf = [decl_line.rstrip()]
        k = method_decl_line
        while k < total and ';' not in lines[k] and '{' not in lines[k]:
            k += 1
            if k < total:
                sig_buf.append(lines[k].rstrip())

        if k < total and ';' in lines[k]:
            sig = ' '.join(l.strip() for l in sig_buf)
            if ')' in sig:
                sig = sig[:sig.rindex(')') + 1]

            results.append({
                "method": method_name,
                "class": class_name,
                "java_file": filepath,
                "java_line_start": ann_start_line + 1,
                "java_line_end": k + 1,
                "has_inline_sql": inline_sql_type is not None,
                "inline_sql_type": inline_sql_type,
                "signature": sig[:150],
            })
            i = k + 1
        else:
            i = method_decl_line + 1

    return results


def parse_mapper_xml(filepath: str, sql_tags=_MAPPER_SQL_TAGS) -> tuple[str | None, list[dict]]:
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    namespace = None
    results = []
    i = 0
    total = len(lines)

    while i < total:
        line = lines[i]

        ns_m = re.search(r'<mapper\s+namespace="([^"]+)"', line)
        if ns_m:
            namespace = ns_m.group(1)
            i += 1
            continue

        for tag in sql_tags:
            m = re.search(rf'<{tag}[\s>][^>]*\bid="([^"]+)"', line)
            if m:
                method_id = m.group(1)
                start_line = i + 1

                close_tag = f'</{tag}>'
                end_line = i
                if close_tag not in line:
                    for j in range(i + 1, total):
                        if close_tag in lines[j]:
                            end_line = j
                            break

                results.append({
                    "method_id": method_id,
                    "sql_type": tag,
                    "xml_file": filepath,
                    "xml_line_start": start_line,
                    "xml_line_end": end_line + 1,
                })
                i = end_line + 1
                break
        else:
            i += 1

    return namespace, results


def build_mapper_index(proj: Project) -> dict:
    mconf = proj.conv["mapper"]
    java_suffixes = tuple(mconf["java_suffixes"])
    xml_suffixes = tuple(mconf["xml_suffixes"])
    sql_tags = tuple(mconf["sql_tags"])
    inline_annotations = tuple(mconf["inline_sql_annotations"])

    # Step 1：解析 Java Mapper 接口
    java_by_class: dict = {}
    java_files = []
    for mapper_root in proj.mapper_java_roots:
        for root, _, files in _walk(mapper_root):
            for fname in files:
                if fname.endswith(java_suffixes):
                    java_files.append(os.path.join(root, fname))

    print(f"[Mapper] 找到 {len(java_files)} 个 Mapper Java 文件，正在解析...")
    for fpath in java_files:
        try:
            entries = parse_mapper_java(fpath, inline_annotations)
            if entries:
                java_by_class[entries[0]["class"]] = entries
        except Exception as e:
            print(f"  [警告] {fpath}: {e}")

    # Step 2：解析 Mapper XML
    xml_by_class: dict = {}
    xml_files = []
    for xml_root in proj.mapper_xml_roots:
        for root, _, files in _walk(xml_root):
            for fname in files:
                if fname.endswith(xml_suffixes):
                    xml_files.append(os.path.join(root, fname))

    print(f"[Mapper] 找到 {len(xml_files)} 个 Mapper XML 文件，正在解析...")
    for fpath in xml_files:
        try:
            namespace, sql_entries = parse_mapper_xml(fpath, sql_tags)
            if namespace:
                xml_by_class[namespace.split('.')[-1]] = sql_entries
        except Exception as e:
            print(f"  [警告] {fpath}: {e}")

    # Step 3：合并
    index: dict = {}
    for class_name, java_methods in java_by_class.items():
        xml_by_id = {e["method_id"]: e for e in xml_by_class.get(class_name, [])}
        for jm in java_methods:
            xml_entry = xml_by_id.get(jm["method"])
            index.setdefault(jm["method"], []).append({
                "method": jm["method"],
                "mapper_class": class_name,
                "signature": jm["signature"],
                "java_file": jm["java_file"],
                "java_line_start": jm["java_line_start"],
                "java_line_end": jm["java_line_end"],
                "sql_type": (jm["inline_sql_type"] or
                             (xml_entry["sql_type"] if xml_entry else None)),
                "has_inline_sql": jm["has_inline_sql"],
                "xml_file": xml_entry["xml_file"] if xml_entry else None,
                "xml_line_start": xml_entry["xml_line_start"] if xml_entry else None,
                "xml_line_end": xml_entry["xml_line_end"] if xml_entry else None,
            })

    sorted_index = dict(sorted(index.items()))
    proj.ensure_index_dir()
    with open(proj.mapper_index_file, "w", encoding="utf-8") as f:
        json.dump(sorted_index, f, ensure_ascii=False, indent=2)

    total_methods = sum(len(v) for v in sorted_index.values())
    print(f"[Mapper] 完成：{total_methods} 个 Mapper 方法，{len(sorted_index)} 个方法名 → {proj.rel(proj.mapper_index_file)}")
    return sorted_index


# ════════════════════════════════════════════════════════════════════════════
# Vue：路由索引（数据库 sys_menu 优先，否则解析 src/router 静态路由）
# ════════════════════════════════════════════════════════════════════════════

def _db_params(proj: Project, cli: dict) -> dict | None:
    """汇总数据库连接参数：CLI 覆盖 > 配置/环境变量。缺关键字段则返回 None。"""
    base = dict(proj.config.get("vue_route_db", {}))
    for k, v in cli.items():
        if v:
            base[k] = v
    # 至少要有库名才认为「配置了 DB 模式」
    if not base.get("database"):
        return None
    base.setdefault("host", "127.0.0.1")
    base.setdefault("port", "3306")
    base.setdefault("table", "sys_menu")
    return base


def _run_mysql(sql: str, db: dict) -> list[dict]:
    cmd = [
        "mysql",
        f"-h{db['host']}", f"-P{db['port']}", f"-u{db.get('user', 'root')}",
    ]
    if db.get("password"):
        cmd.append(f"-p{db['password']}")
    cmd += ["--batch", "--default-character-set=utf8mb4", db["database"], "-e", sql]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[路由] 数据库查询失败：\n{result.stderr.strip()}")
        return []

    lines = result.stdout.strip().splitlines()
    if not lines:
        return []
    headers = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        row = {}
        for i, h in enumerate(headers):
            v = values[i] if i < len(values) else ""
            row[h] = None if v == "NULL" else v
        rows.append(row)
    return rows


def build_route_index_db(proj: Project, vue_root: str, db: dict) -> dict:
    """从 RuoYi sys_menu 表读取动态路由，构建 浏览器路径 → Vue 组件 索引。"""
    table = db["table"]
    sql = (
        "SELECT menu_id, parent_id, menu_name, path, component, menu_type, "
        "perms, visible, status "
        f"FROM {table} WHERE status = '0' ORDER BY parent_id, order_num"
    )
    print(f"[路由] 从数据库 {db['database']}.{table} 读取菜单...")
    rows = _run_mysql(sql, db)
    print(f"[路由] 读取到 {len(rows)} 条菜单记录")
    if not rows:
        return {}

    by_id = {}
    for row in rows:
        try:
            by_id[int(row["menu_id"])] = row
        except (ValueError, TypeError):
            pass

    def parent_of(row):
        pid = row.get("parent_id")
        try:
            pid = int(pid) if pid else 0
        except (ValueError, TypeError):
            pid = 0
        return by_id.get(pid) if pid else None

    def full_path(row) -> str:
        parts, cur = [], row
        while cur:
            p = (cur.get("path") or "").strip("/")
            if p:
                parts.append(p)
            cur = parent_of(cur)
        parts.reverse()
        return "/" + "/".join(parts)

    def breadcrumb(row) -> list:
        names, cur = [], row
        while cur:
            names.append(cur.get("menu_name", ""))
            cur = parent_of(cur)
        names.reverse()
        return names

    index: dict = {}
    for row in rows:
        component = row.get("component")
        if not component or component.strip() in ("Layout", "ParentView", "InnerLink", ""):
            continue

        vue_rel = os.path.join("src", "views", *component.strip("/").split("/")) + ".vue"
        vue_abs = os.path.join(vue_root, vue_rel)
        path = full_path(row)
        entry = {
            "menu_id": int(row["menu_id"]),
            "menu_name": row.get("menu_name", ""),
            "breadcrumb": breadcrumb(row),
            "component": component.strip(),
            "vue_file": os.path.join(proj.rel(vue_root), vue_rel).replace(os.sep, "/"),
            "vue_file_exists": os.path.isfile(vue_abs),
            "perms": row.get("perms"),
            "visible": row.get("visible"),
            "source": "db",
        }
        index.setdefault(path, []).append(entry)
    return index


def parse_vue_router(filepath: str, views_alias: str = "@/views") -> list[dict]:
    """
    字符串安全的 vue-router 静态路由解析器。
    通过括号深度跟踪嵌套，提取 path → 视图组件映射（best-effort）。
    views_alias: 视图组件别名前缀（默认 @/views），只收集指向该前缀的组件。
    """
    views_prefix = views_alias.rstrip("/") + "/"
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        text = f.read()

    n = len(text)
    i = 0
    stack: list[dict] = []   # 每个 {} 对象一帧，记录其 path / component
    results: list[dict] = []
    expect = None            # 'path' | 'component' | None：下一个字符串归属

    def build_full(segs: list[str]) -> str:
        full = ""
        for seg in segs:
            if seg.startswith("/"):
                full = seg.rstrip("/")
            elif full:
                full = full.rstrip("/") + "/" + seg.strip("/")
            else:
                full = "/" + seg.strip("/")
        return full or "/"

    while i < n:
        c = text[i]

        # 行注释
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            nl = text.find('\n', i)
            i = n if nl < 0 else nl
            continue
        # 块注释
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
            continue
        # 字符串
        if c in "'\"`":
            quote = c
            j = i + 1
            buf = []
            while j < n and text[j] != quote:
                if text[j] == '\\' and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            value = "".join(buf)
            i = j + 1

            if expect == "path" and stack:
                stack[-1]["path"] = value
            elif expect == "component" and value.startswith(views_prefix):
                if stack:
                    stack[-1]["component"] = value
            expect = None
            continue
        # 进入对象
        if c == '{':
            stack.append({"path": None, "component": None})
            i += 1
            continue
        # 离开对象：若有组件则产出
        if c == '}':
            if stack:
                frame = stack[-1]
                if frame.get("component"):
                    segs = [fr["path"] for fr in stack if fr.get("path")]
                    results.append({
                        "path": build_full(segs),
                        "component": frame["component"],
                    })
                stack.pop()
            i += 1
            continue
        # 关键字：path: / import( / require(
        if c.isalpha() or c == '_':
            m = re.match(r'[A-Za-z_]\w*', text[i:])
            word = m.group(0)
            i += len(word)
            if word == "path":
                # 跳过空白找冒号
                k = i
                while k < n and text[k] in " \t":
                    k += 1
                if k < n and text[k] == ':':
                    expect = "path"
                    i = k + 1
            elif word in ("import", "require"):
                expect = "component"
            continue

        i += 1

    return results


def _resolve_route_files(vue_root: str, project_root: str, files) -> list:
    """把用户手动指定的路由文件解析成实际存在的绝对路径。
    相对路径优先相对 Vue 前端根（与 @/views 一致），其次相对项目根；也支持绝对路径。"""
    resolved = []
    for f in files or ():
        if not f:
            continue
        cands = [f] if os.path.isabs(f) else [
            os.path.join(vue_root, f),
            os.path.join(project_root, f),
        ]
        hit = next((c for c in cands if os.path.isfile(c)), None)
        if hit:
            ab = os.path.abspath(hit)
            if ab not in resolved:
                resolved.append(ab)
        else:
            print(f"[路由] [警告] 手动指定的路由文件未找到：{f}")
    return resolved


def build_route_index_static(proj: Project, vue_root: str, extra_files=()) -> dict:
    """解析路由目录下的静态路由文件（可追加手动指定的路由文件），构建路由索引（无需数据库）。"""
    vconf = proj.conv["vue"]
    views_alias = vconf["views_alias"]
    router_dir = os.path.join(vue_root, *vconf["router_dir"].strip("/").split("/"))
    router_files = []
    if os.path.isdir(router_dir):
        for root, _, files in _walk(router_dir):
            for fname in files:
                if fname.endswith((".js", ".ts")):
                    router_files.append(os.path.join(root, fname))
    else:
        single = os.path.join(vue_root, "src", "router.js")
        if os.path.isfile(single):
            router_files.append(single)

    # 手动指定的路由文件：覆盖自动扫描到不了的非标准位置
    # （如 JeecgBoot 的 src/config/router.config.js）
    manual = _resolve_route_files(vue_root, proj.root, extra_files)
    for fpath in manual:
        if fpath not in router_files:
            router_files.append(fpath)
    if manual:
        print(f"[路由] 手动指定 {len(manual)} 个路由文件（含非标准位置）")

    print(f"[路由] 静态模式：解析 {len(router_files)} 个路由文件...")
    index: dict = {}
    for fpath in router_files:
        try:
            for r in parse_vue_router(fpath, views_alias):
                comp = r["component"]                      # @/views/xxx
                rel_view = comp[len("@/"):]                 # views/xxx
                vue_rel = os.path.join("src", *rel_view.split("/")) + ".vue"
                vue_abs = os.path.join(vue_root, vue_rel)
                entry = {
                    "menu_id": None,
                    "menu_name": "",
                    "breadcrumb": [],
                    "component": comp,
                    "vue_file": os.path.join(proj.rel(vue_root), vue_rel).replace(os.sep, "/"),
                    "vue_file_exists": os.path.isfile(vue_abs),
                    "perms": None,
                    "visible": None,
                    "source": "static",
                    "router_file": proj.rel(fpath),
                }
                index.setdefault(r["path"], []).append(entry)
        except Exception as e:
            print(f"  [警告] {fpath}: {e}")
    return index


def build_route_index(proj: Project, cli_db: dict, route_files=()) -> dict:
    """构建前端路由索引：每个 Vue 根优先 DB 模式，未配置则回退静态模式。
    无论哪种模式，手动指定的路由文件（route_files）都会被静态解析并并入。"""
    merged: dict = {}
    db = _db_params(proj, cli_db)

    for vue_root in proj.vue_roots:
        if db:
            part = build_route_index_db(proj, vue_root, db)
            if not part:  # DB 不可用 → 回退静态（含手动文件）
                print("[路由] 数据库无结果，回退静态路由解析")
                part = build_route_index_static(proj, vue_root, route_files)
            elif route_files:  # DB 有结果，仍并入手动指定的静态路由文件
                for path, entries in build_route_index_static(proj, vue_root, route_files).items():
                    part.setdefault(path, []).extend(entries)
        else:
            part = build_route_index_static(proj, vue_root, route_files)

        for path, entries in part.items():
            merged.setdefault(path, []).extend(entries)

    sorted_index = dict(sorted(merged.items()))
    proj.ensure_index_dir()
    with open(proj.route_index_file, "w", encoding="utf-8") as f:
        json.dump(sorted_index, f, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in sorted_index.values())
    mode = "数据库" if db else "静态"
    print(f"[路由] 完成（{mode}模式）：{total} 条路由，{len(sorted_index)} 个路径 → {proj.rel(proj.route_index_file)}")
    return sorted_index


# ════════════════════════════════════════════════════════════════════════════
# 查询
# ════════════════════════════════════════════════════════════════════════════

def load_json(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _require_index(index: dict | None, name: str) -> dict:
    if index is None:
        print(f"索引不存在，请先运行：python3 code_index.py --build\n（缺失：{name}）")
        sys.exit(1)
    return index


def _path_matches(query: str, pattern: str) -> bool:
    q_parts = query.strip("/").split("/")
    p_parts = pattern.strip("/").split("/")
    if len(q_parts) != len(p_parts):
        return False
    return all(
        p == q
        or (p.startswith("{") and p.endswith("}") and q)
        or (p.startswith(":") and q)
        for q, p in zip(q_parts, p_parts)
    )


# ── API 查询 ──────────────────────────────────────────────────────────────────

def query_api(search_term: str, index: dict) -> bool:
    """返回是否命中。"""
    if search_term in index:
        print(f"\n[接口·精确] {search_term}")
        for e in index[search_term]:
            print(f"  HTTP方法: {e['method']}")
            print(f"  文件:     {e['file']}")
            print(f"  行号:     第 {e['line_start']} 行 ~ 第 {e['line_end']} 行")
        return True

    if search_term.startswith('/'):
        var_matches = [(k, v) for k, v in index.items() if _path_matches(search_term, k)]
        if var_matches:
            print(f"\n[接口·路径变量] {search_term}")
            for path, entries in var_matches:
                for e in entries:
                    print(f"\n  匹配模式: {path}")
                    print(f"  HTTP方法: {e['method']}")
                    print(f"  文件:     {e['file']}")
                    print(f"  行号:     第 {e['line_start']} 行 ~ 第 {e['line_end']} 行")
            return True

    matches = [(k, v) for k, v in index.items() if search_term.lower() in k.lower()]
    if not matches:
        return False
    print(f"\n[接口·模糊] 找到 {len(matches)} 个结果：")
    for path, entries in matches:
        for e in entries:
            print(f"\n  路径:     {path}")
            print(f"  HTTP方法: {e['method']}")
            print(f"  文件:     {e['file']}")
            print(f"  行号:     第 {e['line_start']} 行 ~ 第 {e['line_end']} 行")
    return True


# ── 方法查询 ──────────────────────────────────────────────────────────────────

def query_method(method_name: str, index: dict):
    method_name = method_name.rstrip('()')
    if method_name in index:
        entries = index[method_name]
        print(f"\n[方法] {method_name} —— 找到 {len(entries)} 处定义：")
        for e in entries:
            print(f"\n  类名:   {e['class']}")
            print(f"  签名:   {e['signature']}")
            print(f"  文件:   {e['file']}")
            print(f"  行号:   第 {e['line_start']} 行 ~ 第 {e['line_end']} 行")
        return

    matches = [(k, v) for k, v in index.items() if method_name.lower() in k.lower()]
    if not matches:
        print(f"未找到方法 '{method_name}'")
        return
    print(f"\n[方法·模糊] 找到 {sum(len(v) for _, v in matches)} 处定义：")
    for mname, entries in matches:
        for e in entries:
            print(f"\n  方法名: {mname}")
            print(f"  类名:   {e['class']}")
            print(f"  签名:   {e['signature']}")
            print(f"  文件:   {e['file']}")
            print(f"  行号:   第 {e['line_start']} 行 ~ 第 {e['line_end']} 行")


# ── Mapper 查询 ───────────────────────────────────────────────────────────────

def _print_mapper_entries(entries: list[dict]):
    for e in entries:
        print(f"\n  Mapper类:  {e['mapper_class']}")
        print(f"  签名:      {e['signature']}")
        print(f"  Java接口:  {e['java_file']}")
        print(f"             第 {e['java_line_start']} 行 ~ 第 {e['java_line_end']} 行", end="")
        if e['has_inline_sql']:
            print(f"  ← 内联 @{e['sql_type'].capitalize()} SQL")
        else:
            print()
        if e['xml_file']:
            print(f"  XML文件:   {e['xml_file']}")
            print(f"             第 {e['xml_line_start']} 行 ~ 第 {e['xml_line_end']} 行  [{e['sql_type'].upper()}]")
        else:
            kind = '内联SQL' if e['has_inline_sql'] else '仅 MyBatis-Plus 基础方法'
            print(f"  XML文件:   无（{kind}）")


def query_mapper(method_name: str, index: dict):
    method_name = method_name.rstrip('()')
    if method_name in index:
        print(f"\n[Mapper] {method_name} —— 找到 {len(index[method_name])} 处定义：")
        _print_mapper_entries(index[method_name])
        return

    matches = [(k, v) for k, v in index.items() if method_name.lower() in k.lower()]
    if not matches:
        print(f"未找到 Mapper 方法 '{method_name}'")
        return
    print(f"\n[Mapper·模糊] 找到 {sum(len(v) for _, v in matches)} 处定义：")
    for mname, entries in matches:
        print(f"\n  ── {mname} ──")
        _print_mapper_entries(entries)


# ── 路由查询 ──────────────────────────────────────────────────────────────────

def _print_route_entry(path: str, e: dict):
    flag = "✓ 文件存在" if e["vue_file_exists"] else "✗ 文件不存在"
    bc = " > ".join(e["breadcrumb"]) if e.get("breadcrumb") else ""
    if bc:
        print(f"\n  菜单路径:   {bc}")
    else:
        print()
    print(f"  浏览器路由: {path}")
    print(f"  组件标识:   {e['component']}")
    print(f"  Vue文件:    {e['vue_file']}  [{flag}]")
    if e.get("perms"):
        print(f"  权限标识:   {e['perms']}")
    if e.get("source") == "static" and e.get("router_file"):
        print(f"  来源:       静态路由 {e['router_file']}")


def query_route(search_term: str, index: dict) -> bool:
    if search_term in index:
        print(f"\n[路由·精确] {search_term}")
        for e in index[search_term]:
            _print_route_entry(search_term, e)
        return True

    if search_term.startswith("/"):
        var_matches = [(k, v) for k, v in index.items() if _path_matches(search_term, k)]
        if var_matches:
            print(f"\n[路由·路径变量] {search_term} —— 找到 {len(var_matches)} 条：")
            for path, entries in var_matches:
                for e in entries:
                    _print_route_entry(path, e)
            return True

    kw = search_term.lower()
    matches = []
    for k, entries in index.items():
        for e in entries:
            if (kw in k.lower()
                    or kw in e["component"].lower()
                    or kw in (e.get("menu_name") or "").lower()
                    or kw in " > ".join(e.get("breadcrumb") or []).lower()):
                matches.append((k, e))
                break
    if not matches:
        return False
    print(f"\n[路由·模糊] 找到 {len(matches)} 条结果：")
    for path, e in matches:
        _print_route_entry(path, e)
    return True


# ── 智能查询（默认行为）：接口 + 路由 ─────────────────────────────────────────

def query_smart(search_term: str, proj: Project):
    api_index = load_json(proj.api_index_file)
    route_index = load_json(proj.route_index_file)

    if api_index is None and route_index is None:
        print("索引不存在，请先运行：python3 code_index.py --build")
        sys.exit(1)

    hit = False
    if api_index is not None and query_api(search_term, api_index):
        hit = True
    if route_index is not None and query_route(search_term, route_index):
        hit = True
    if not hit:
        print(f"未找到匹配 '{search_term}' 的接口或路由。")
        print("  · 查 Java 方法： --method <名称>")
        print("  · 查 Mapper：    --mapper <名称>")


# ── 列表 ──────────────────────────────────────────────────────────────────────

def list_apis(proj: Project):
    index = _require_index(load_json(proj.api_index_file), "api_index.json")
    print(f"共 {sum(len(v) for v in index.values())} 个接口：\n")
    for path, entries in sorted(index.items()):
        for e in entries:
            print(f"  [{e['method']:6}] {path}")
            print(f"           {proj.rel(e['file'])}  L{e['line_start']}-{e['line_end']}")


def list_routes(proj: Project):
    index = _require_index(load_json(proj.route_index_file), "route_index.json")
    total = sum(len(v) for v in index.values())
    print(f"共 {total} 条路由（{len(index)} 个路径）：\n")
    for path, entries in sorted(index.items()):
        for e in entries:
            flag = "✓" if e["vue_file_exists"] else "✗"
            print(f"  [{flag}] {path}")
            print(f"       {e['vue_file']}")


# ════════════════════════════════════════════════════════════════════════════
# 构建 + 体检
# ════════════════════════════════════════════════════════════════════════════

def cmd_build(proj: Project, cli_db: dict, route_files=()):
    if not proj.has_java() and not proj.has_vue():
        print("未检测到可索引的项目结构（Java src/main/java 或 Vue src/views）。")
        print("请把 code_index.py 放到项目根目录，或用 --project 指定路径。")
        sys.exit(1)

    print(f"[初始化] 项目根目录：{proj.root}")
    if proj.has_java():
        print(f"[初始化] Java 模块：{len(proj.java_roots)} 个，Mapper XML：{len(proj.mapper_xml_roots)} 个")
    if proj.has_vue():
        print(f"[初始化] Vue 前端：{len(proj.vue_roots)} 个")
    print()

    built = []
    if proj.has_java():
        build_api_index(proj);    print()
        build_method_index(proj); print()
        build_mapper_index(proj); print()
        built += ["api", "method", "mapper"]
    if proj.has_vue():
        build_route_index(proj, cli_db, route_files); print()
        built.append("route")

    manifest = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project_root": proj.root,
        "java_roots": [proj.rel(r) for r in proj.java_roots],
        "vue_roots": [proj.rel(r) for r in proj.vue_roots],
        "indices": built,
    }
    proj.ensure_index_dir()
    with open(proj.manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[完成] 索引已写入 {proj.rel(proj.index_dir)}/（建议加入 .gitignore）")


def cmd_doctor(proj: Project):
    print(f"项目根目录: {proj.root}\n")
    print("检测结果:")
    print(f"  Java 后端: {'是' if proj.has_java() else '否'}")
    for r in proj.java_roots:
        print(f"    · {proj.rel(r)}")
    print(f"  Mapper XML 目录: {len(proj.mapper_xml_roots)} 个")
    print(f"  Vue 前端: {'是' if proj.has_vue() else '否'}")
    for r in proj.vue_roots:
        print(f"    · {proj.rel(r)}")

    print("\n前端路由配置:")
    db = _db_params(proj, {})
    if db:
        masked = {**db, "password": "***" if db.get("password") else "(空)"}
        print(f"  数据库模式: 已配置 → {masked['user']}@{masked['host']}:{masked['port']}/{masked['database']} (表 {masked['table']})")
    else:
        print("  数据库模式: 未配置（将使用静态路由解析）")
        print(f"  如需 DB 模式：复制 code-index.ini.example 为 {CONFIG_FILE_NAME} 并填写连接信息")

    cfg_files = proj.config.get("route_files", [])
    if cfg_files:
        print("  手动指定的路由文件（配置/环境变量）:")
        for vue_root in (proj.vue_roots or [proj.root]):
            for f in cfg_files:
                resolved = _resolve_route_files(vue_root, proj.root, [f])
                mark = "✓" if resolved else "✗ 未找到"
                print(f"    · {f}  [{mark}]")
    else:
        print("  手动指定路由文件: 无（路由不在 src/router 时，用 --route-file 或配置 [vue_route_static] files）")

    jc, mc, vc = proj.conv["java"], proj.conv["mapper"], proj.conv["vue"]
    ann = ", ".join(f"{k}:{v}" for k, v in jc["mapping_annotations"].items())
    print("\n生效约定（可在 code-index.ini 的 [scan]/[java]/[mapper]/[vue] 覆盖）:")
    print(f"  [java]   源码根标志: {jc['source_marker']}   源文件后缀: {', '.join(jc['source_suffixes'])}")
    print(f"  [java]   Controller 后缀: {', '.join(jc['controller_suffixes'])}   类前缀注解: @{jc['class_mapping_annotation']}")
    print(f"  [java]   映射注解: {ann}")
    print(f"  [mapper] 接口后缀: {', '.join(mc['java_suffixes'])}   XML 根: {mc['xml_marker']}   SQL 标签: {', '.join(mc['sql_tags'])}")
    print(f"  [mapper] 内联 SQL 注解: {', '.join(mc['inline_sql_annotations'])}")
    print(f"  [vue]    前端根标志: {', '.join(vc['root_markers'])}   路由目录: {vc['router_dir']}   视图别名: {vc['views_alias']}")
    extra = proj.conv["scan"]["ignore_dirs"]
    if extra:
        print(f"  [scan]   追加忽略目录: {', '.join(extra)}")

    print("\n已有索引:")
    for label, path in [
        ("接口", proj.api_index_file), ("方法", proj.method_index_file),
        ("Mapper", proj.mapper_index_file), ("路由", proj.route_index_file),
    ]:
        idx = load_json(path)
        if idx is None:
            print(f"  {label}: 未构建")
        else:
            print(f"  {label}: {len(idx)} 条 ({proj.rel(path)})")


# ════════════════════════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════════════════════════

_HELP_EPILOG = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 构建索引（自动检测 Java 后端 / Vue 前端）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python3 code_index.py --build
  python3 code_index.py --build --project /path/to/project
  python3 code_index.py --doctor          查看检测结果与配置状态

  索引输出到 .code-index/，每次新增/修改代码后重新 --build 即可更新。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 查询
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python3 code_index.py /sys/user/list     接口 / 前端路由（智能匹配）
  python3 code_index.py userList           模糊匹配
  python3 code_index.py --method selectById   Java 方法定义
  python3 code_index.py --mapper selectById   Mapper 接口 + XML SQL
  python3 code_index.py --route p_user_0001   仅查前端路由
  python3 code_index.py --list api            列出全部接口
  python3 code_index.py --list route          列出全部路由

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 前端路由数据库模式（RuoYi sys_menu）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  默认走静态路由解析（无需数据库）。若路由全在 sys_menu 表中，
  复制 code-index.ini.example 为 code-index.ini 并填写连接信息，
  或用环境变量 CODE_INDEX_DB_* / 命令行 --host --port --db --user --password。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 前端静态路由：手动指定路由文件（自动扫描不到时）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  路由不在 src/router（如 JeecgBoot 的 src/config/router.config.js）时：
    python3 code_index.py --build --route-file src/config/router.config.js
  多个文件用逗号或多次 --route-file；路径相对 Vue 前端根或绝对路径。
  也可在 code-index.ini 配 [vue_route_static] files=...，或环境变量 CODE_INDEX_ROUTE_FILES。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 配置驱动适配（适配不同目录结构 / 命名 / 注解，无需改源码）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python3 code_index.py --doctor      查看当前生效的全部约定
  在 code-index.ini 的 [scan]/[java]/[mapper]/[vue] 段逐项覆盖默认约定，例如：
    [java]
    controller_suffixes = Controller.java, Api.java
  详见 README「配置驱动适配」与 code-index.ini.example。
"""


def main():
    parser = argparse.ArgumentParser(
        description="code-index —— 通用代码索引工具（Java/Spring + RuoYi-Vue），快速定位代码位置",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("query", nargs="?", help="接口路径或关键词（默认智能查询接口+路由）")
    parser.add_argument("--build", action="store_true", help="构建/更新索引（自动检测项目类型）")
    parser.add_argument("--doctor", action="store_true", help="显示检测结果与配置状态")
    parser.add_argument("--method", metavar="NAME", help="查询 Java 方法定义（模糊）")
    parser.add_argument("--mapper", metavar="NAME", help="查询 Mapper 方法（Java 接口 + XML SQL）")
    parser.add_argument("--route", metavar="NAME", help="仅查询前端路由")
    parser.add_argument("--api", metavar="NAME", help="仅查询后端接口")
    parser.add_argument("--list", metavar="WHAT", nargs="?", const="api",
                        choices=["api", "route"], help="列出全部接口(api)或路由(route)")
    parser.add_argument("--project", metavar="DIR", help="项目根目录，默认脚本所在目录")
    parser.add_argument("--config", metavar="FILE", help="指定配置文件，默认项目根目录的 code-index.ini")
    parser.add_argument("--route-file", metavar="FILE", action="append",
                        help="手动指定前端路由配置文件（相对 Vue 前端根或绝对路径；可重复或逗号分隔）。"
                             "用于路由不在 src/router 的项目，如 JeecgBoot 的 src/config/router.config.js")
    # 数据库连接覆盖（最高优先级）
    parser.add_argument("--host", help="数据库地址")
    parser.add_argument("--port", help="数据库端口")
    parser.add_argument("--db", help="数据库名")
    parser.add_argument("--user", help="数据库用户")
    parser.add_argument("--password", help="数据库密码")
    args = parser.parse_args()

    project_root = os.path.abspath(args.project) if args.project else _SCRIPT_DIR
    config = load_config(project_root, args.config)
    # 配置中追加的忽略目录，在扫描（构建 Project）前生效
    EXTRA_IGNORE_DIRS.update(config.get("conv", {}).get("scan", {}).get("ignore_dirs", []))
    cli_db = {
        "host": args.host, "port": args.port, "database": args.db,
        "user": args.user, "password": args.password,
    }
    # 手动指定的路由文件：配置文件/环境变量（load_config 已收集） + 命令行（逗号或多次）
    route_files = list(config.get("route_files", []))
    for item in (args.route_file or []):
        route_files += [s.strip() for s in item.split(",") if s.strip()]

    # 是否需要扫描源码目录（构建 / 体检 / 智能查询时需要）
    need_scan = bool(args.build or args.doctor or args.query or args.list)
    proj = Project(project_root, config, scan=need_scan)

    if args.build:
        cmd_build(proj, cli_db, route_files)
    elif args.doctor:
        cmd_doctor(proj)
    elif args.method:
        query_method(args.method, _require_index(load_json(proj.method_index_file), "method_index.json"))
    elif args.mapper:
        query_mapper(args.mapper, _require_index(load_json(proj.mapper_index_file), "mapper_index.json"))
    elif args.route:
        query_route(args.route, _require_index(load_json(proj.route_index_file), "route_index.json"))
    elif args.api:
        query_api(args.api, _require_index(load_json(proj.api_index_file), "api_index.json")) or \
            print(f"未找到匹配 '{args.api}' 的接口")
    elif args.list:
        (list_apis if args.list == "api" else list_routes)(proj)
    elif args.query:
        query_smart(args.query, proj)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
