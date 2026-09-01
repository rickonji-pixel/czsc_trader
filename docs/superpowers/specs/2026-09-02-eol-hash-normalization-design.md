# LF与可移植身份哈希治理设计

日期：2026-09-02
分支：`codex/eol-hash-normalization`

## 1. 目标

消除项目内CRLF、LF和mixed换行长期共存造成的无意义差异，并使冻结规则、来源制品和实验档案的身份验证不再依赖操作系统或工作区换行格式。

本次治理不改变策略参数、信号、行情数值、研究结论和回测语义。

## 2. 根因

- 本机Git设置`core.autocrlf=true`，仓库缺少统一的普通文本EOL规则；
- `.gitattributes`只为少量身份文件指定CRLF，其他文本由本机配置决定；
- 身份校验存在三种实现：JSON语义哈希、LF归一化文本哈希、工作区原始字节哈希；
- 新文件可能在提交前为LF、重新检出后为CRLF，原始字节哈希随之变化；
- 部分工作区文件已显示为mixed。

## 3. 仓库换行规则

`.gitattributes`采用以下原则：

```gitattributes
* text=auto eol=lf
data/raw/*.csv -text
*.png binary
*.jpg binary
*.jpeg binary
*.gif binary
*.pdf binary
*.db binary
```

普通源码、JSON、Markdown、配置和研究文本制品在所有平台检出为LF。`data/raw/*.csv`继续保持原始字节，因为行情manifest明确记录其字节哈希。常见二进制格式明确标记为binary。

删除现有所有单文件`eol=crlf`例外。仓库规则覆盖用户级`core.autocrlf`。

## 4. 统一身份模型

新增`src/czsc_trader/identity.py`，提供三种且仅三种身份函数：

1. `canonical_json_sha256(payload_or_path)`：解析JSON对象，按键排序、紧凑UTF-8序列化后计算SHA-256；用于规则、策略配置和JSON来源制品；
2. `normalized_text_sha256(path)`：将CRLF和孤立CR统一为LF后计算SHA-256；用于CSV、Markdown、Python等文本制品；
3. `raw_file_sha256(path)`：直接计算文件字节；仅用于行情CSV和二进制制品。

JSON语义身份忽略缩进、键顺序和换行；文本身份只忽略换行差异；原始字节身份完全保留。

## 5. 模块迁移

### 5.1 规则基线

- 基线文件继续使用JSON语义身份，现有版本SHA不变；
- 四层策略来源从原始字节哈希改为JSON语义身份，并用语义相等替代字节完全相同；
- regime策略来源从LF文本哈希改为JSON语义身份；
- 更新活动注册表中的两个`source_sha256`，不改策略内容。

### 5.2 执行规则

- 活动执行规则文件改用JSON语义身份；
- EX02冻结来源改用独立的JSON语义身份；
- 更新执行规则注册表；
- 标的、信号基线版本和信号基线SHA绑定规则保持不变。

### 5.3 实验档案

- 继续对普通文本做LF归一化；
- 复用共享身份函数；
- `__pycache__`和`.pyc`继续作为运行态内容排除；
- 历史manifest及实验制品不重写。

### 5.4 行情数据

- `data/raw/*.csv`和manifest中行情文件哈希继续使用原始字节；
- 数据发布、验证和历史行情文件不迁移。

## 6. 迁移边界

允许修改：

- `.gitattributes`；
- 共享身份模块及其调用方；
- 两个活动注册表中的技术性来源哈希；
- 测试和使用文档。

保持不变：

- `experiments/*`历史档案内容与清单；
- `data/raw/*`；
- 基线和执行规则JSON的策略字段；
- `state/`、`outputs/`和本地运行态文件。

## 7. 验收

1. 同一JSON使用LF、CRLF、不同缩进和不同键顺序时语义哈希一致；
2. 同一普通文本使用LF、CRLF和CR时文本哈希一致；
3. 原始字节哈希对换行变化保持敏感；
4. 活动基线与活动执行规则正常解析；
5. 全部48个实验档案通过验证；
6. `git check-attr`显示普通文本`eol=lf`、行情CSV`text=unset`；
7. 在独立临时检出中活动身份验证通过；
8. `git diff --check`无换行或空白错误。

## 8. 防回归

唯一端到端测试文件增加：

- 三类身份函数行为测试；
- 基线来源和执行规则来源的跨换行验证；
- 仓库EOL属性代表性检查；
- 临时目录中的LF/CRLF JSON解析与身份一致性；
- 全部实验档案验证继续保留。
