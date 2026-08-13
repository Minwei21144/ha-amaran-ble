# Amaran BLE v0.5.0-beta.106

## English

This beta focuses on reliable reacquisition through an ESPHome Bluetooth
Proxy when only part of an imported Sidus Mesh remains powered.

### Highlights

- Connection rounds now contain recently reachable proxies instead of every
  configured fixture. Powered-off fixtures no longer strand the connector in
  a permanent no-candidate state.
- Reconnects wait for ESPHome to confirm that the previous GATT allocation is
  reusable, with bounded cleanup if release confirmation is delayed.
- Candidate failover, sole-survivor retries, GATT authorization recovery,
  scanner-local device continuity and per-session fixture availability are
  covered by regression tests.
- Existing beta.4 controls and the imported Mesh JSON format remain
  compatible.

### Qualification

| Topology | Result | Mean | Median | P90 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2 of 4 powered, 10 cycles | 10/10 | 58.04 s | 46.65 s | 91.8 s | 112.8 s |
| 1 of 4 powered, 3 cycles | 3/3 | 63.7 s | 47.5 s | n/a | 105.1 s |

- Home Assistant commands: 92/92 successful.
- Mesh writes: 184/184 successful.
- Offline tests: 208/208 passed.

This remains a beta. Three-of-four, four-of-four, clean Core cold-start and
extended retention qualification are still pending. A cold-start observation
blocked by an unrelated integration was excluded because Home Assistant never
emitted the startup event that begins Amaran BLE ownership.

## 繁體中文

此 beta 主要改善 ESPHome Bluetooth Proxy 在既有 Sidus Mesh 只有部分燈具
通電時的重新接管穩定度。

### 重點

- 候選輪次改由近期可達 Proxy 組成，不再讓斷電但仍存在設定中的燈具造成永久
  no-candidate 狀態。
- 重新連線前等待 ESPHome 確認先前 GATT allocation 已可重用；釋放確認延遲時
  使用有界 cleanup。
- 候選切換、唯一存活者重試、GATT authorization 恢復、同 scanner 裝置連續性
  與每次 session 的燈具可用性，都有回歸測試保護。
- 完整保留 beta.4 控制功能與既有 Mesh JSON 匯入格式。

### 驗證結果

| 拓撲 | 結果 | 平均 | 中位數 | P90 | 最大值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2/4 通電，十輪 | 10/10 | 58.04 秒 | 46.65 秒 | 91.8 秒 | 112.8 秒 |
| 1/4 通電，三輪 | 3/3 | 63.7 秒 | 47.5 秒 | 不適用 | 105.1 秒 |

- Home Assistant 命令：92/92 成功。
- Mesh 寫入：184/184 成功。
- 離線測試：208/208 通過。

本版仍為 beta。3/4、4/4、乾淨 Core 冷啟動與長時間 retention 尚待完整驗證。
其中一次冷啟動觀察受到其他整合阻塞，Home Assistant 沒有發出啟動 Amaran BLE
接管所需的 startup event，因此不列入本版連線時間。
