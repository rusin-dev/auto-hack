# auto-hack

全自动化 Codeforces 的 Div.3 公开 Hack 对拍工具。

用 tree-sitter 解析标程的输入结构，自动构造测试数据，并行跑「标程 vs 被测程序」的
对拍，按 WA / TLE / MLE / RE 判定，把能打倒被测程序的数据落成可直接提交的 hack 用例。

## 安装

需要 Python 3.10+ 和一个 C++ 编译器（MinGW-w64 / MSYS2 的 g++；老版本 g++ 不认识
`-std=c++17` 时会自动逐级降级重试）。

```bash
pip install -r requirements.txt
```

## 快速开始

1. 新建题目目录，放入两份源码：

   ```
   test/T1/std.cpp     # 标程（正确答案）
   test/T1/hack.cpp    # 要被 hack 的程序
   ```

2. 运行 `python main.py`，选题号 → 配置向导会用 AST 列出识别到的输入变量，逐个问取值
   范围，再问时间/内存限制、轮数、线程数与生成策略，配置存成 `test/T1/config.yaml`。
3. 对拍在新开的独立窗口里跑（带实时进度条），Ctrl+C 随时停。
4. 命中结果落在 `hack/T1/`：`001.in` / `001.out` / `hits.txt`，并复制一份 `std.cpp`、
   `hack.cpp`、`config.yaml` 便于回看。每次运行会先清掉上一次的 `.in/.out`。

## 常用命令

```bash
python main.py --list                  # 列出 test/ 下的题目与不可用原因
python main.py -p T1                   # 用已有 config.yaml 开拍（新窗口）
python main.py -p T1 -r 0              # 无休止对拍
python main.py -p T1 -r 5000 -s 42     # 指定轮数与随机种子（可复现）
python main.py -p T1 -j 4              # 4 线程并行
python main.py -p T1 --strategy edge   # 只用边界值生成
python main.py -p T1 --no-window       # 不开新窗口，在当前窗口跑
python main.py -p T1 --reconfig        # 重跑配置向导
python main.py -p T1 --no-mle -y       # 关掉 MLE 检查，跳过确认
```

轮数 / 命中数填 `0` 表示不限制。退出码：`0` 找到 hack 点，`3` 没找到，`2` 中途中止。

## 数据生成策略

`--strategy`（或 config 里的 `strategy`）三档：

| 策略 | 行为 | 适用 |
| --- | --- | --- |
| `mixed` | 约 35% 取值取边界（端点、`0/1/-1`、`int`/`long long` 溢出值），20% 把整块数组填成同一个值 | 默认，命中率与多样性的折中 |
| `edge` | 每个值都从边界池取，数组常整块全相等 | 专打边界与退化结构 |
| `random` | 区间内均匀随机 | 想要「正常数据」压测 / 复现老行为 |

hack 点绝大多数住在边界和退化结构上。实测一个「只有 `n=1` 才错」的程序：
`random` 跑 200 轮 0 命中，`mixed` 第 3 轮命中，`edge` 第 1 轮命中。

## 判定口径

- **WA**：与 Codeforces 的 token checker 一致，按任意空白切分后逐 token 比较；行内多空格、
  Tab、CRLF、每行一个数与一行多个数、末尾空行都不算差异。
- **TLE**：被测程序超过 `tl_ms` 判 TLE（额外给 0.2s 余量）。关掉 TLE 检查时把超时放宽到
  10 倍，慢程序才不会既判不出 TLE 也判不出 WA/RE。
- **MLE**：Windows 读 `GetProcessMemoryInfo` 的峰值工作集，Linux 读 `/proc` 的 `VmHWM`，
  其余平台轮询 `ps`。只做事后测量，不设硬上限；拿不到峰值内存时会在报告里提示「这些轮
  相当于没判 MLE」。
- **RE**：被测程序非零退出或崩溃。
- 标程自己超时只会跳过该轮并计数（多半是数据规模填大了）；标程运行错误则中止，并把触发
  数据一并打印。

## 图论 / 复杂结构

向导里选「需要自己提供数据生成器」，把 `gen.cpp` 或 `gen.py` 放进题目目录即可。每次生成
数据会调用它一次，并追加轮号作为第 1 个参数（生成器不认参数时会自动退回无参调用），
stdout 的内容作为这轮的输入，超时 15s。

## 开发与测试

```bash
python -m unittest discover -s tests -t .   # 单元测试（含编译运行的端到端用例）
ruff check autohack tests main.py            # lint（配置见 pyproject.toml）
```

`test/`、`hack/`、`build/` 都在 `.gitignore` 里：题目数据与对拍产物只留在本地。

## 已知限制

- 面向 Windows 控制台（独立窗口、VT 渲染、窗口尺寸自愈），其他平台只保证基本可用。
- 只做本地对拍与数据落盘，不代你向 Codeforces 提交。
- 输入结构靠 AST 识别，`cin`/`scanf`/`getline` 之外的读入（自定义快读、`fread` 解析）
  认不出来，请改用自定义生成器。
- 不比较浮点误差：两边输出按字符串精确到 token 比较，`double` 题需要自己保证输出格式一致。
