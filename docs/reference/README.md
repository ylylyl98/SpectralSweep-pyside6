# 旧实验参考资料

本目录收纳原来散落在项目根目录的 Notebook 和旧脚本。文件内容原样保留，
用于查阅历史实验方法；当前 SpectralSweep 桌面程序使用根目录的
[`launch.bat`](../../launch.bat) 启动，不依赖这些参考文件。

| 原根目录文件 | 现在的位置 |
| --- | --- |
| `attoDRY2100_magnet_control_attocube.ipynb` | [notebooks/attoDRY2100_magnet_control_attocube.ipynb](notebooks/attoDRY2100_magnet_control_attocube.ipynb) |
| `lf6_LM-PL-1000.ipynb` | [notebooks/lf6_LM-PL-1000.ipynb](notebooks/lf6_LM-PL-1000.ipynb) |
| `lf6_LM-PL_Andor.ipynb` | [notebooks/lf6_LM-PL_Andor.ipynb](notebooks/lf6_LM-PL_Andor.ipynb) |
| `iv_automation -1000 magnet control.py` | [scripts/iv_automation -1000 magnet control.py](<scripts/iv_automation -1000 magnet control.py>) |
| `05_Manual APS100 magnet power supply.pdf` | [仪器手册](<../manuals/05_Manual APS100 magnet power supply.pdf>) |
| `spectrum_overlay_smoke.png` | [界面参考截图](../screenshots/spectrum_overlay_smoke.png) |

## 继续使用旧 Notebook

移动目录不会自动调整 Jupyter 内核的工作目录。Notebook 里存在项目根目录的
模块导入和相对 CSV 输出路径。需要继续使用时，在执行原有代码前，先用单独
单元格恢复原来的项目根目录，并确认打印出的路径：

```python
from pathlib import Path
import os
import sys

project_root = next(
    (p for p in (Path.cwd(), *Path.cwd().parents)
     if (p / "launch.bat").is_file() and (p / "main.py").is_file()),
    None,
)
if project_root is None:
    raise RuntimeError("请先将内核工作目录设置到 SpectralSweep-pyside6 项目内")
os.chdir(project_root)
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
print("工作目录：", Path.cwd())
```

这只恢复本次整理前的工作目录与项目模块查找路径。旧 Notebook 还引用了本仓库
未提供的历史模块（例如 `spectral_experiments`、`backend.instruments`），以及
旧电脑的 SDK 和数据绝对路径；继续运行时仍需按原实验环境配置。此次整理没有
执行 Notebook 或改写其仪器控制、数据路径及输出。
