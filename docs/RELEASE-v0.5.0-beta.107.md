# Amaran BLE 0.5.0-beta.107

## English

Source update dated 2026-09-11. No beta.107 Git tag or GitHub Release is
created by this update; the latest tagged HACS release remains beta.106.

### Root cause and fix

After an ESPHome API reconnect or scanner replacement, the previous GATT
backend can retain a retired slot tracker. Its allocation list no longer
receives updates, even when the current Home Assistant scanner reports free
slots. In beta.106, the pending-release gate could repeatedly inspect this
old object and block every new connection attempt indefinitely.

Beta.107 resolves the currently registered scanner for the original ESPHome
source on each cleanup/recovery check. It reads that scanner's allocation
snapshot instead of waiting on the retired tracker's notification future.
Busy, missing, malformed or wrong-source snapshots still block new GATT
requests. The fix does not blindly discard a pending allocation after a timer.

Diagnostics now expose the snapshot status, source and cumulative recovery
wait time through the existing privacy-redaction path. Candidate selection,
Mesh login, GATT/CCCD timeouts and fixture controls are unchanged. No HA
Bluetooth, ESPHome firmware, scanner settings or automation changes are needed.

### Comparison

| Behavior | beta.106 | beta.107 |
| --- | --- | --- |
| Allocation evidence after scanner replacement | Can retain retired backend tracker | Re-resolves the current same-source scanner |
| Waiting for a free slot | Can await a retired tracker's future | Rechecks current allocation evidence within the existing cleanup deadline |
| Missing or mismatched current evidence | Does not resolve current scanner | Keeps the no-new-GATT guard |
| GATT/CCCD retries and connection-time guarantee | Variable; no guarantee | Unchanged; still variable |
| Offline test suite | 208 passed | 219 passed, including 11 new regression tests |

### Validation and limitations

Offline coverage includes scanner replacement, old-busy/current-free and
old-free/current-busy trackers, delayed scanner registration, invalid snapshots,
source mismatch, lookup failure and preventing a new GATT request while pending.

Physical checks used one ESPHome Bluetooth Proxy and two T2c fixtures online
in a four-configured-fixture Mesh. HA Core check passed. After a Core restart,
the third connection attempt succeeded; the first two had GATT timeouts.

| Control-switch cycle | Attempts until success | Successful attempt only | First sampled `connected` after switch-on |
| --- | ---: | ---: | ---: |
| 1 | 1 | 12.310 s | Within approximately 68 s |
| 2 | 1 | 9.302 s | Within approximately 43 s |
| 3 | 9 | 18.222 s | Within approximately 357 s |

The successful-attempt column is not total reconnection latency. The last
column is a polling upper bound, not an exact transition timestamp. All three
cycles eventually recovered, with 1%/2% brightness state feedback verified on
both fixtures. Release/cleanup samples used `current_scanner`, reported all
three slots free and left no pending slot recovery. No additional Core or
Proxy restart was used between these three cycles.

The third cycle still encountered GATT timeouts, a CCCD authorization failure
and an advertisement-resynchronization miss before recovery. This release
does not resolve that long tail or establish long-term stability. Initial
scanner-replacement recovery is covered by offline regression tests; the
physical cycles are not repeated fault injection of the original incident.
Multiple ESPHome Proxy devices have not been physically qualified here.
Historical beta.106 timings were measured under different conditions and
should not be treated as a controlled speed comparison with these three runs.

## 繁體中文

2026-09-11 原始碼更新。本次不建立 beta.107 Git Tag 或 GitHub Release；
已建立 Tag 的 HACS 發行版仍為 beta.106。

### 根本原因與修正

ESPHome API 重新連線或 scanner 被替換後，先前 GATT backend 可能仍保留
舊槽位追蹤物件。即使 HA 目前的 scanner 已回報空槽位，舊物件的 allocation
清單也不再更新。beta.106 的 pending-release 保護會反覆讀取這個舊物件，
導致永久阻止所有新的連線嘗試。

beta.107 在每次清理／恢復檢查時，依原本 ESPHome 來源查詢目前註冊的
scanner，讀取它的槽位快照，不再等待舊追蹤物件的通知 future。目前槽位
忙碌、資料缺失、格式異常或來源不符時，仍禁止新 GATT 請求；不會只因
計時器到期就直接丟棄待釋放的槽位紀錄。

診斷新增快照狀態、來源及累計恢復等待時間，沿用既有隱私遮蔽。候選選擇、
Mesh 登入、GATT／CCCD timeout 與燈光控制均不變。不需要修改 HA Bluetooth、
ESPHome 韌體、掃描設定或 automation。

### 橫向比較

| 行為 | beta.106 | beta.107 |
| --- | --- | --- |
| scanner 替換後的槽位依據 | 可能持續讀取舊 backend 追蹤物件 | 重新查詢相同來源目前的 scanner |
| 等待空槽位 | 可能等待舊追蹤物件的 future | 在原有清理期限內重新檢查目前槽位資料 |
| 目前資料缺失或來源不符 | 不查詢目前 scanner | 保留禁止新 GATT 的保護 |
| GATT／CCCD 重試與連線時間保證 | 有波動，不保證固定時間 | 未變更，仍有波動 |
| 離線測試 | 208 項通過 | 219 項通過，包含新增 11 項回歸測試 |

### 驗證與限制

離線回歸涵蓋 scanner 替換、舊物件忙碌但目前空閒、舊物件空閒但目前忙碌、
scanner 延遲註冊、異常快照、來源不符、查詢失敗，以及 pending 時不送新
GATT 請求。

實機使用一顆 ESPHome Bluetooth Proxy，四燈設定中兩盞 T2c 上線。HA Core
check 通過；Core 重啟後第三次連線嘗試成功，前兩次發生 GATT timeout。

| 總開關循環 | 成功前總嘗試數 | 僅最後成功嘗試耗時 | 開啟後首次取樣到 connected |
| --- | ---: | ---: | ---: |
| 1 | 1 | 12.310 秒 | 約 68 秒內 |
| 2 | 1 | 9.302 秒 | 約 43 秒內 |
| 3 | 9 | 18.222 秒 | 約 357 秒內 |

「成功嘗試耗時」不等於整體重新連線時間；最後一欄是輪詢取樣的時間上限，
並非精確狀態切換時刻。三輪均最終恢復，兩盞燈的 1%／2% 亮度狀態回報
驗證成功。釋放／清理取樣均使用 `current_scanner`，回報三個空槽位，且
沒有殘留 pending slot recovery。三輪之間沒有額外重啟 Core 或 Proxy。

第三輪恢復前仍遇到 GATT timeout、CCCD authorization 錯誤及廣播重新同步
未找到裝置。本版尚未解決這些長尾延遲，也不代表已完成長期穩定性驗證。
原始 scanner 替換故障由離線回歸覆蓋；實機循環不是反覆注入同一故障。
此次也沒有多顆 ESPHome Proxy 的實機驗證。beta.106 歷史測試條件不同，
不應將歷史連線時間與本次三輪直接視為受控的速度比較。
