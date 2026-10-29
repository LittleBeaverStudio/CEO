# 小河狸首席执行官

本地经营驾驶舱，用于查看现金跑道、应收应付、流水台账和经营周报。银行流水、发票明细及配置保存在本机应用数据目录。

## 启动

安装 Python 3.11 或更新版本后，在项目目录运行：

```powershell
python -m pip install -r requirements.txt
python desktop_launcher.py
```

Windows 用户也可以运行 `启动首席执行官.bat`。应用默认在本机端口 8878 提供服务；启动器会在该端口占用时选择可用端口。

## 只读 Skill

`skill/littlebeaver-ceo-assistant/` 包含用于读取本机服务状态、公司列表和各公司最新经营周报的配套 Skill。可分发文件位于 `dist_skills/littlebeaver-ceo-assistant.skill`。接口仅监听本机访问且不提供写入能力。

## 验证

```powershell
python tests/test_smoke_http.py
python tests/test_e2e.py
```
