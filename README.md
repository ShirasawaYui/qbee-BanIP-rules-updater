# QBEE IP 规则更新工具

用于从两个公开规则来源抓取 IP 段，清理后合并成 QBEE 可使用的 `Ban.dat`。项目只使用 Python 标准库，不需要额外安装第三方依赖。

## 功能

- 从腾讯文档“对策B：屏蔽涉及的IP段”代码块中严格提取规则，生成 `DamnYou.txt`。不会把更新时间表格或其他章节中的 IP 当作结果。
- 从 [BTN-Collected-Rules](https://github.com/PBH-BTN/BTN-Collected-Rules) 下载规则，移除注释和空行，生成 `BTN.txt`。
- 识别 IPv4/IPv6 单地址、CIDR 和起止范围；精确计算两个来源的并集，去除重复、包含、重叠和相邻区间，生成 `Ban.dat`。
- 通过 `update_Ban.bat` 并行抓取两个来源；只有两个抓取任务都成功时才执行合并，避免用不完整数据覆盖输出。

## 环境

- Windows
- Python 3.10 或更高版本
- 运行时需要访问腾讯文档和 GitHub 原始文件地址

脚本均为标准库实现，无需 `pip install`。批处理会按顺序尝试项目内 `.venv`、系统 Python、`py -3`，以及可发现的 Codex Python。

## 快速开始

在项目根目录打开 PowerShell：

```powershell
# 一键更新：并行抓取，成功后合并
.\update_Ban.bat
或者
.\update_Ban.ps1
```

也可以分步执行：

```powershell
python scrape_DamnYou.py --verbose
python scrape_BTN.py --verbose
python merge_Ban.py
```

合并脚本默认读取当前目录的 `DamnYou.txt` 和 `BTN.txt`，输出 `Ban.dat`。也可以指定文件：

```powershell
python merge_Ban.py first.txt second.txt --output custom-Ban.dat
```

抓取脚本支持 `--url`、`--output` 和 `--timeout` 参数；使用 `--help` 查看完整说明。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试不依赖外网：抓取测试使用本地 HTTP 服务和构造的文档载荷；合并测试覆盖 IPv4、IPv6、CIDR、范围、重叠、相邻区间、错误输入和原子写入。

## 输出与安全性

- 文本输出使用 UTF-8 和 LF 换行。
- 写入采用临时文件再原子替换；抓取或解析失败时保留旧输出。
- 合并不会扩大输入范围，只输出两个来源的精确 IP 并集。

## 来源

- [PBH-BTN/BTN-Collected-Rules](https://github.com/PBH-BTN/BTN-Collected-Rules)
- [腾讯文档规则页](https://docs.qq.com/doc/DQnJBTGJjSFZBR2JW)

## 许可证

本项目代码采用 [MIT License](LICENSE) 发布。抓取所得规则仍受各自来源的条款约束。
