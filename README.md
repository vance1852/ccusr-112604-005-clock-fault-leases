# 校时故障中的期限判断与席位恢复

这里保存一套可脱机使用的合成运维资料。两个事件文件各自从相同清册开始，模拟两次独立运行。

## 清册和事件

`samples/inventory.json` 记录演练开始时已知的资源、对象与约定。可信时间源按独立域标注。每个分区的 policy 给出独立域最少数、单份观测有效时长。ticks 表示来源重启后自己的观测刻度；期限与 true_min/true_max 使用同一种演练时间基准。席位的 generation 区分历次占有权。

`samples/shift-log.jsonl` 和 `samples/independent-time.jsonl` 每行是一个 JSON 对象。通用字段 `at` 是资料到达值班侧的演练秒，`kind` 标明来源记录的类型。同一秒内保持给定文件顺序。事实发生时间由记录自身字段提供。observation 的 bounds 是在 action_time 对可信时间的估计。练习中同一观察有效期内，两个时间基准以相同秒长走时，所以相对 action_time 的经过秒数也用于平移该区间。仅允许以尚在有效期内、覆盖本分区的观测作证。boot 变更后该主机旧观测不能当作新观测。effect 可以携带历次 generation，并有 unknown/committed/void 三种确认状态。

文件可包括申请、已发指令、状态观测和外部回执。出现于记录中的申请或意图可能未获准，外部回执可能迟到或与此前观测冲突。

## 浏览资料

在仓库根目录用 Python 3.11 运行。只需标准库。

```sh
python3 sample_reader.py samples/shift-log.jsonl
python3 -m unittest discover -s tests -v
```

原始资料可按 `--kind` 筛选。程序按接收顺序显示记录。单元测试核对资料能够读取，以及事件夹具的基本结构。
