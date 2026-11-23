# 修缮工程验工事实仓

把师傅报量、监理实测、合同清单、签认与阶段支付凭据组织成**可持续刷新、可回查证据**的验工事实仓。
`data/facts.json` 为主资料（含复测改段、签认锁定、尚未批准的合同变更），`data/scenarios.json`
为覆盖其余业务规则的情景资料。仅依赖 Python 3.11+ 标准库。

## 业务口径如何落地

1. **报量 → 实测 → 签认的证据链**：报量按合同指定部位/工作项/计量单位落到米程区间；
   签认量沿"合同部位 ∩ 当时实测支持范围"自左向右落位，只有签认覆盖的量进入付款依据。
2. **复测三动作**（`facthouse/support.py`）：`full` 全套替代（保留各实测段边界作为分界证据）、
   `partial` 仅修订部分实测（未覆盖依据原样保留）、`withdraw` 撤回一条签认。
3. **同一实体多工序**：不同工作项可分别计量；同一可支付工作项的重叠量标记争议，
   不得重复结算。跨分包同工作项的边界重叠无裁决时保留争议范围，其余独立范围照常通过。
4. **计价双轴**：合同/变更按生效期选价，现场签认时间决定进入哪份付款表，两者独立判断。
   待批变更只进待认（held），不按新价付款，也不占用将来获批可支付的量；审批事件到达后
   同一幂等键由待认转补入。
5. **锁定付款表不可变**：锁定表只做快照保留与核对，重算不覆盖数量、单价和签认链。
6. **更正台账**：复测造成的转段/转分包/价差，在第一个可入账期按实际受影响合同项逐行列
   冲减（clawback）与补入（add/held），禁止两家分包轧差；每条带稳定幂等键，复跑不重复冲。
7. **双向追查**：从任一笔调整键或签认号可追出两边各自使用的报量、签认、签认时实测版本、
   当前实测版本与被替代版本链（`facthouse.ledger.FactHouse.trace`）。

## 目录

- `facthouse/`：事实仓引擎（区间运算、版本演化、覆盖裁决、计价、台账、追查）
- `data/`：原始事实资料（不可变输入）
- `tests/`：资料完整性与业务规则测试
- `scripts/refresh_facthouse.py`：隔离模式刷新/追查入口
- `build/`：刷新产物（不入库）

## 运行

```bash
# 刷新事实仓
python3 -I scripts/refresh_facthouse.py --facts data/facts.json --out build/facthouse.json

# 带上一版产物复跑（幂等：已冲差额不重复冲，待认可随审批转付）
python3 -I scripts/refresh_facthouse.py --facts data/facts.json \
    --ledger build/facthouse.json --out build/facthouse.json

# 从调整键或签认号追查两边验工依据
python3 -I scripts/refresh_facthouse.py --facts data/facts.json --trace CERT-81

# 测试
python3 -I -m unittest discover -s tests
```

脚本以 `-I` 隔离模式运行，`sys.path` 在脚本内显式注入项目根目录，避免数据目录中的
同名模块被解释器加载。

## 输出结构（`build/facthouse.json`）

- `reports`：每条报量的已签认/撤回/未签认米数
- `independent_passing_ranges_m`：无争议且受支持、照常通过的独立范围
- `correction_ledger`：冲减/补入分列的更正台账（含幂等键、金额、证据链）
- `held_quantities`：待认量及原因（待批变更/归属争议/无已批价格线）
- `payment_tables`：各付款期付款表（锁定表带 `locked: true`）
- `disputes`：争议范围与原因码
- `reconciliation_issues`：与资料中更正提示的对账差异（主资料应为空）
