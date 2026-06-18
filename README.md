# code-index

> 通用代码索引工具 —— 一条命令定位接口 / 方法 / Mapper / 前端路由的代码位置。

把单个 `code_index.py` 丢进项目根目录，运行 `python3 code_index.py --build`，它会**自动检测项目类型**并构建索引。之后无需在海量代码里全文搜索，一条命令就能拿到「文件 + 行号」。

专为 **AI 编程助手（Claude Code / Cursor 等）+ 人**快速定位代码而设计：把"接口路径 / 方法名 / 路由"翻译成精确的文件位置，省去反复 grep。

## 适用项目

| 类型 | 识别方式 | 产出索引 |
|------|---------|---------|
| Java / Spring Boot / MyBatis 后端 | 存在 `src/main/java`（支持多模块 Maven） | 接口路径、Java 方法、Mapper 接口 + XML SQL |
| RuoYi 风格 Vue 前端 | 存在 `src/views` 或 `src/router` | 浏览器路由 → Vue 组件文件 |

前后端可以在同一个仓库（monorepo），工具会一次性把两边都索引好。

## 特性

- **零依赖**：只用 Python 3 标准库（前端 DB 模式可选依赖 `mysql` 命令行客户端）。
- **单文件**：只有一个 `code_index.py`，复制即用。
- **自动检测**：自动发现多模块 Maven 的每个 `src/main/java`、Mapper 目录、Vue 前端根。
- **框架预设 + 配置驱动**：`--profile jeecg/jhipster/...` 一行套用一类项目约定；目录结构 / 命名 / 注解也可在 `code-index.ini` 逐项覆盖，适配不同项目而**无需改工具源码**（详见下文）。
- **路径归一**：索引时折叠重复斜杠（源码手误的 `//foo` 与 `/foo` 都能命中），前端路由自动去重。
- **凭据外置**：数据库连接信息走配置文件 / 环境变量 / 命令行，**绝不写进源码**。
- **智能匹配**：精确 → 路径变量（`/user/{id}`、`/ra/:accountId`）→ 关键词模糊，逐级回退。

## 快速开始

```bash
# 1. 把 code_index.py 复制到项目根目录
cp code_index.py /path/to/your-project/

# 2. 构建索引（自动检测项目类型）
cd /path/to/your-project
python3 code_index.py --build

# 3. 查询
python3 code_index.py /sys/user/list        # 查接口 / 前端路由（智能匹配）
python3 code_index.py --method selectUserById   # 查 Java 方法
python3 code_index.py --mapper selectById       # 查 Mapper（接口 + XML SQL）
python3 code_index.py --route p_user_0001       # 查前端路由
```

也可以不复制，用 `--project` 指定项目根目录：

```bash
python3 code_index.py --build --project /path/to/your-project
```

## 命令详解

### 构建 / 检测

```bash
python3 code_index.py --build       # 构建/更新全部索引（每次改完代码重新跑即可）
python3 code_index.py --doctor      # 显示检测到的项目结构、路由模式、已有索引状态
```

索引文件统一输出到项目根目录的 `.code-index/`（建议加入 `.gitignore`）：

```
.code-index/
├── api_index.json       接口路径索引
├── method_index.json    Java 方法索引
├── mapper_index.json    Mapper 接口 + XML SQL 索引
├── route_index.json     前端路由索引
└── manifest.json        构建元信息（时间、检测到的模块）
```

### 查询

| 目的 | 命令 |
|------|------|
| 接口 / 前端路由（智能，默认） | `python3 code_index.py /sj/sjZyProject/list2` |
| 接口 / 路由（模糊） | `python3 code_index.py sjZyProject` |
| 仅后端接口 | `python3 code_index.py --api userList` |
| 仅前端路由 | `python3 code_index.py --route p_user_0001` |
| Java 方法定义 | `python3 code_index.py --method queryList2` |
| Mapper 方法（Java 接口 + XML SQL） | `python3 code_index.py --mapper selectById` |
| 列出全部接口 | `python3 code_index.py --list api` |
| 列出全部路由 | `python3 code_index.py --list route` |

查询支持三级匹配，自动逐级回退：

1. **精确**：`/sj/sjZyProject/list2`
2. **路径变量**：`/sj/sjZyProject/count/2024` 能命中 `/sj/sjZyProject/count/{projectYear}`
3. **关键词模糊**：`sjZyProject` 命中所有含该词的路径；中文菜单名也能搜（前端路由）

## 前端路由：三种模式

前端路由可能来自不同地方，工具都支持：

### 静态模式（默认，无需数据库）

扫描 `src/router/` 下的路由文件，用字符串安全的词法扫描器提取 `path → @/views/...` 映射（不依赖具体变量名，`constantRoutes`/`dynamicRoutes`/`asyncRouterMap` 等都能解析）。**开箱即用，不需要任何配置**。适合路由写在 `src/router/` 里的项目。

### 手动指定路由文件（路由不在 `src/router/` 时）

有些项目把路由表写在非标准位置，自动扫描覆盖不到，例如 **JeecgBoot** 把路由写在 `src/config/router.config.js`。这时手动指定该文件即可（解析逻辑与静态模式一致，会并入索引）：

**方式一：命令行**（可重复 / 逗号分隔，路径相对 Vue 前端根或绝对路径）

```bash
python3 code_index.py --build --route-file src/config/router.config.js
python3 code_index.py --build --route-file src/config/router.config.js,src/config/other.js
```

**方式二：配置文件**（`code-index.ini`）

```ini
[vue_route_static]
files = src/config/router.config.js
```

**方式三：环境变量**（逗号分隔）

```bash
export CODE_INDEX_ROUTE_FILES=src/config/router.config.js
python3 code_index.py --build
```

> 手动指定的文件在静态 / 数据库模式下都会被解析并并入路由索引。`python3 code_index.py --doctor` 可查看已配置的路由文件及是否找到。

### 数据库模式（RuoYi sys_menu）

RuoYi 的业务页面路由通常存在数据库 `sys_menu` 表里（后端动态下发），前端路由文件里查不到。这种情况配置数据库连接后，工具会从 `sys_menu` 读取完整菜单树，给出**面包屑路径 + 权限标识 + 组件文件**。

配置任一方式即可启用 DB 模式（优先级：命令行 > 环境变量 > 配置文件）：

**方式一：配置文件**（复制 `code-index.ini.example` 为 `code-index.ini`）

```ini
[vue_route_db]
host = 127.0.0.1
port = 3306
database = your-db
user = your-user
password = your-password
table = sys_menu
```

**方式二：环境变量**

```bash
export CODE_INDEX_DB_HOST=127.0.0.1
export CODE_INDEX_DB_NAME=your-db
export CODE_INDEX_DB_USER=your-user
export CODE_INDEX_DB_PASSWORD=your-password
python3 code_index.py --build
```

**方式三：命令行**

```bash
python3 code_index.py --build --host 127.0.0.1 --db your-db --user u --password p
```

> 配置了 `database` 即视为启用 DB 模式；DB 连不上时会自动回退到静态模式。

## 框架预设（Profile）：一行套用一类项目约定

不想逐项配置时，用 `--profile` 一行套用某类框架的约定预设：

```bash
python3 code_index.py --build --profile jeecg     # JeecgBoot：自动带上 src/config/router.config.js 等
python3 code_index.py --build --profile jhipster  # JHipster：Controller 识别 *Resource.java
```

也可写进配置文件（持久生效）：

```ini
[project]
profile = jeecg
```

内置预设：

| profile | 适用 | 预设内容 |
|---------|------|---------|
| `ruoyi` | RuoYi-Vue | 默认约定；路由走 `sys_menu`(DB) 或 `src/router` |
| `jeecg` | JeecgBoot | 默认约定 + 路由文件 `src/config/router.config.js` |
| `jhipster` | JHipster | Controller 识别 `*Resource.java` / `*Controller.java` |

> 优先级：内置默认 < profile < 显式 `[java]/[mapper]/[vue]` 段 < 环境变量 < 命令行。
> 即 profile 给你一套起点，仍可在其上用下面的配置项逐条微调。`--profile` 也可用 `--doctor` 核对。

## 配置驱动适配：覆盖默认约定

工具内置了一套默认约定（Spring Boot + MyBatis + RuoYi/JeecgBoot-Vue），**不写任何配置即可开箱即用**。当你的项目用了不同的目录结构 / 命名约定 / 注解时，在 `code-index.ini` 里覆盖对应项即可适配，**无需改工具源码**。

先用 `--doctor` 查看当前生效的全部约定：

```bash
python3 code_index.py --doctor
```

可覆盖的约定（每项的值即内置默认）：

```ini
[scan]
# 额外忽略的目录（并入内置忽略表），逗号分隔
ignore_dirs = generated, third_party

[java]
source_marker = src/main/java                 # 后端源码根标志（相对各模块）
source_suffixes = .java                        # 方法索引扫描的源文件后缀
controller_suffixes = Controller.java          # Controller 文件后缀，可多个
class_mapping_annotation = RequestMapping      # 类级路径前缀注解
mapping_annotations = GetMapping:GET, PostMapping:POST, PutMapping:PUT, DeleteMapping:DELETE, PatchMapping:PATCH, RequestMapping:ANY

[mapper]
java_suffixes = Mapper.java                    # Mapper 接口文件后缀，可多个
xml_marker = src/main/resources/mapper         # Mapper XML 根标志
xml_suffixes = .xml
sql_tags = select, insert, update, delete      # XML 里的 SQL 标签
inline_sql_annotations = Select, Insert, Update, Delete

[vue]
root_markers = src/views, src/router           # 前端根标志目录（任一存在即为前端根）
router_dir = src/router                         # 自动扫描的静态路由目录
views_alias = @/views                           # 视图组件别名前缀
```

**常见适配示例**

- Controller 命名不是 `*Controller`（如 `*Api`、`*Resource`、`*Endpoint`）：
  ```ini
  [java]
  controller_suffixes = Controller.java, Api.java, Resource.java
  ```
- Mapper 接口叫 `*Dao`：
  ```ini
  [mapper]
  java_suffixes = Mapper.java, Dao.java
  ```
- 后端源码不在 `src/main/java`（非 Maven 布局）：
  ```ini
  [java]
  source_marker = app/src
  ```
- 前端视图别名是 `@/pages`：
  ```ini
  [vue]
  views_alias = @/pages
  ```

> 配置项是「逐项覆盖」：只写你要改的，其余仍用默认。改完跑 `--build` 即生效，`--doctor` 可核对。

## ⚠️ 凭据安全

- 工具**绝不在源码中硬编码任何数据库账号密码**。
- 真实配置文件 `code-index.ini` 含密码，已在 `.gitignore` 中忽略，**不要提交到版本库**。
- 仓库里只提供不含密码的 `code-index.ini.example` 模板。

## 给 AI 助手用的建议

如果用 Claude Code / Cursor 等，可以在项目的 `CLAUDE.md` / 规则文件里加一段，引导它优先用本工具定位代码，而不是全文搜索：

```markdown
## 代码搜索规则
定位后端接口 / 方法 / Mapper / 前端路由时，优先使用项目根目录的 code_index.py：
- 查接口：       python3 code_index.py <HTTP路径或关键词>
- 查 Java 方法： python3 code_index.py --method <方法名>
- 查 Mapper：    python3 code_index.py --mapper <方法名>
- 查前端路由：   python3 code_index.py --route <路由或组件名>
新增/修改代码后用 python3 code_index.py --build 更新索引。
```

## 工作原理

- **接口索引**：扫描 `*Controller.java`，解析类级 `@RequestMapping` 前缀与方法级 `@GetMapping/@PostMapping/...`，拼出完整 HTTP 路径。
- **方法索引**：扫描全部 `.java`，用大括号配平定位每个方法的起止行。
- **Mapper 索引**：解析 `*Mapper.java` 接口方法，关联同名 `Mapper.xml` 里的 `<select|insert|update|delete id="...">`，也识别 `@Select` 等内联 SQL。
- **路由索引**：DB 模式读 `sys_menu` 递归拼路径；静态模式用字符串安全的扫描器解析 vue-router 嵌套结构。

均为轻量正则 / 词法扫描，不做完整语法分析——胜在快、零依赖、对绝大多数常规写法足够准。

## 局限性

- 面向常规写法。高度动态拼接的路由、注解里用常量引用的路径、非常规代码风格可能漏掉。
- 静态路由解析是 best-effort；路由不在 `src/router/` 时用 `--route-file` 手动指定；路由全在数据库（如 RuoYi `sys_menu`、JeecgBoot 后端动态下发的菜单）时，前端文件里查不到，需用 DB 模式或直接看后端菜单数据。
- 目前聚焦 Java/Spring 后端 + Vue 前端（RuoYi / JeecgBoot 等）。工具按「自动检测 + 可手动指定」的思路设计，欢迎按需扩展到更多框架 / 语言。

## License

MIT —— 详见 [LICENSE](LICENSE)。
