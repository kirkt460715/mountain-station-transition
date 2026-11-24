# 山区观测覆盖估计与分期退站签认

这些演练台账是本地合成资料，使用虚构编号。CSV 采用 UTF-8 和带表头的逗号分隔格式。同一单元内用竖线列出的值表示多项或候选集合，含义见各表说明。空值通常表示未记录或不适用，明确的零会保留为 0。

## 资料

- `data/demands.csv`  原独立巡查归档的观察机会。stratum 界定能否相互代表的片区与天气。need 为可用于对应险情判断的要素组合。
  字段为 `area / stratum / need / minimum / independent`。
- `data/stations.csv`  计划启用点及运行中的旧站。feed 是供电依赖，uplink 是回传依赖。REF 仅供观察参照。
  字段为 `station / areas / elements / feed / uplink / kind`。
- `data/works.csv`  工程组提供的阶段预约。begin_by 和 end_by 为允许施工期的整数日，span 是实际工期。依赖列为前置工程。retire 完工后不可回滚。
  字段为 `work / kind / station / begin_by / end_by / span / crew / depends`。
- `data/resources.csv`  每天可同时使用的工位。相同类型的工程共同占用对应数量。
  字段为 `crew / count`。
- `data/field_updates.csv`  field 为巡查更正的站点属性，reported_day 为业务收到该记录的时点。
  字段为 `id / reported_day / effective_day / station / field / value`。
- `data/trial_reports.csv`  工程试运行回单。known_severe 是有独立参照的重雨机会数，unclassified 为参照缺失的机会。
  字段为 `report / work / day / known_severe / unclassified`。
- `data/trial_evidence.csv`  各阶段勘测回单关联的现场观察机会。reported_day 表示回单送达日，现场与归档时间见 opportunities 和 readings。
  字段为 `report / opportunity / reported_day`。
- `data/opportunities.csv`  独立观察机会及参照状态。day 按工程首日计算，负数为历史对照。
  字段为 `opportunity / area / stratum / day / reference`。
- `data/readings.csv`  独立巡测与重叠期逐站要素记录，有效期仍需看实际布设。缺报或部分要素完整均允许。paired_archive 为历史外场同验，field_check 为工程季外场归档。
  字段为 `opportunity / station / valid_elements / reported_day / origin`。

## 核对台账

环境为 Python 3.11.2，只使用标准库。在仓库根目录运行下列命令检查文件格式、表头和基础外键。输出为各张源表可读行数。

```sh
python3 input_audit.py
python3 -m unittest discover -s tests -v
```

可在第一条命令后指定另一个输入目录，目录中的同名 CSV 沿用上述格式。列名和基础引用清单见 `input_contract.json`。

## 覆盖与分期退站

`station_coverage/` 在既有台账之上做覆盖估计与退站签认，不写回任何源表。

- `python3 -m station_coverage.cli [台账目录] [--show SUMMARY] [--out 快照.json]`
  读取同名 CSV 并输出阶段安排。`--show` 可选 `summary`（默认）、`schedule`、
  `stages`、`retirements`、`field`、`common-mode`、`trials`、`json`。
- 退出码 `0` 表示台账自身没有对不上的地方且所有退站都可签认；`1` 表示存在
  保留结论或台账问题（原因会打印）；`2` 表示台账无法读取。

判定口径（与上面的资料说明一一对应）：

- 一次机会以 `opportunities` 的片区、日期和降水类别为准。`reference=indeterminate`
  的机会既不计成功也不计漏测，单独列为证据空白；没有可判机会的片区记为
  `no_evidence`，不能拿"等几场雨"当作已通过。
- 新站只有在与工程关联的 `trial_evidence` 上取得参照可判的暴雨证明，并经
  `trial_reports` 送达后才计入覆盖；日常小雨不替代暴雨，普通降雨可由暴雨证明
  推出，反向不成立。回单登记的机会数会与关联行核对，对不上时给出问题。
- 独立备份同时看供电与回传：共用任意一条链的两座站最多算一路（最大匹配）。
  同批退站与单条链断开都会单独推演，以区分"仅失去一处观测"与"多个站同时消失"。
- 现场更正按 `effective_day` 解释实际状态；业务在 `reported_day` 才知道，之前按
  旧值作出的安排会在 `knowledge_events` 中列出并标出受影响的退站工程。
- 退站工程的开工日由前置工程、工位容量和覆盖可签认性共同决定；排不进窗口就保留
  原站，并分别说明是资源、覆盖下限、证据不足还是共同故障在阻止。

`tests/test_station_coverage.py` 覆盖上述规则，并演示下一批资料直接替换
`data/` 即可重算，不需要改代码。
