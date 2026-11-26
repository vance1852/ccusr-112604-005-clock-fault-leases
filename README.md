# 校时故障中的期限判断与席位恢复

这里保存一套可脱机使用的合成运维资料，以及一个只依赖标准库的 Python 值班能力（`duty` 包）。
两个事件文件各自从相同清册开始，模拟两次独立运行。

## 清册和事件

`samples/inventory.json` 记录演练开始时已知的资源、对象与约定。可信时间源按独立域标注。每个分区的 policy 给出独立域最少数、单份观测有效时长。ticks 表示来源重启后自己的观测刻度；期限与 true_min/true_max 使用同一种演练时间基准。席位的 generation 区分历次占有权。

`samples/shift-log.jsonl` 和 `samples/independent-time.jsonl` 每行是一个 JSON 对象。通用字段 `at` 是资料到达值班侧的演练秒，`kind` 标明来源记录的类型。同一秒内保持给定文件顺序。事实发生时间由记录自身字段提供。observation 的 bounds 是在 action_time 对可信时间的估计。练习中同一观察有效期内，两个时间基准以相同秒长走时，所以相对 action_time 的经过秒数也用于平移该区间。仅允许以尚在有效期内、覆盖本分区的观测作证。boot 变更后该主机旧观测不能当作新观测。effect 可以携带历次 generation，并有 unknown/committed/void 三种确认状态。

文件可包括申请、已发指令、状态观测和外部回执。出现于记录中的申请或意图可能未获准，外部回执可能迟到或与此前观测冲突。

## 判定语义（保守优先）

证据准入与可信时间（`duty/trust.py`）：

1. 观测必须覆盖目标分区；决策时刻距 `action_time` 不超过该分区 `freshness`（含边界秒）；
2. 主机发生 boot 变更后，其旧 boot 的观测不能再作证；
3. 准入观测按**可信域**去重计数，独立域数量达到 `source_domains_min` 才足以作为期限依据；
4. 准入观测的平移区间取**交集**：交集为空即证据冲突，整个分区保守隔离；
5. 交集相对到期点给出 `before / after / crossing` 三种位置，跨过期点同样隔离。

三类时限动作（`duty/engine.py`）：

- **renew 续用**：只有可信区间整段位于到期点之前才允许。某代际一旦被证明到期，
  之后即使观测因向后校时回到到期前也**不得重新允许**（单调事实，且该拦截优先于冲突判定）；
- **reclaim 回收**：只有确证到期（区间整段在到期点或之后）才允许；
  不能证实到期的席位绝不交给另一个任务；
- **claim 新领取**：分区可核验 + 席位确证空闲 + 无同代际在先申请（资源可承诺）才通过；
  generation 围栏拒绝过期代际和跳代领取。

回执与恢复：

- `unknown` 之后迟到的 `committed/void` 是澄清，必须接受；已有确定结论后再来的回执按重复登记异常；
- 迟到回执按 generation 围栏：席位已被新代际领取时，旧主机的通知标记 `superseded`，不改变新归属；
- 恢复欠账区分两类活跃工作：执行机**已接收但未报提交**（unknown 桶，按当前证据判断能否幂等重派）
  与**从未发出**的申请（用最新证据重新判定）。结论分四桶：`redrive` 可安全补做、
  `approve` 现已满足条件可推进、`review` 仍无法判定留值班复核、`waiting` 继续等待回执或新证据。

## 命令行

在仓库根目录用 Python 3.11 运行，只需标准库：

```sh
# 浏览原始资料
python3 sample_reader.py samples/shift-log.jsonl

# 初始化值班状态目录（持久化到独立目录，原始资料保持只读）
python3 -m duty.cli init samples/inventory.json --state .state/shift

# 接入新的校时观测、续用申请或执行回执（JSONL 文件或单条 JSON）
python3 -m duty.cli ingest samples/shift-log.jsonl --state .state/shift

# 查询入口：当前可通过/暂缓/复核的操作、分区隔离、席位有效使用权
python3 -m duty.cli ops --state .state/shift
python3 -m duty.cli partitions --state .state/shift
python3 -m duty.cli seats --state .state/shift
python3 -m duty.cli explain a9 --state .state/shift

# 实际推进一个已通过的续用/回收/领取
python3 -m duty.cli advance b5 --state .state/shift

# 恢复欠账：先 dry-run 看方案，确认后 --apply 落地
python3 -m duty.cli recover --state .state/shift
python3 -m duty.cli recover --state .state/shift --apply

# 一次连续操作里看到时间源异常、分区隔离、重启和恢复
python3 -m duty.cli run samples/inventory.json samples/shift-log.jsonl --state .state/demo

# 中途停止服务后重启：快照直接载入，并用追加日志重放核对旧决定与证据
python3 -m duty.cli verify --state .state/shift
```

状态目录包含固化的 `inventory.json`、追加写的 `journal.jsonl`（全部事件与值班动作）
和原子替换的 `snapshot.json`。删除快照后 `verify`/重开仍能仅凭日志完整重建。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

- `tests/test_sources.py`：原始资料可读性与事件顺序（随仓库提供）；
- `tests/test_judgement.py`：两份合成事件上的端到端判定、重启作废、恢复分桶与迟到回执围栏；
- `tests/test_rules.py`：向后校时不可重开续用、跨过期点隔离、freshness 边界、
  generation 围栏、两类欠账恢复与持久化重放。
