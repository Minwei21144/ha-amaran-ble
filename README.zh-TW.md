# Amaran BLE for Home Assistant

這是一個非官方 Home Assistant 自訂整合，透過 Home Assistant Bluetooth 或
ESPHome Bluetooth Proxy，在本機控制既有的 Amaran / Sidus BLE Mesh。

目前 `main` 原始碼為 `0.5.0-beta.107`，修正 ESPHome scanner 重建後永久
等待舊槽位追蹤物件的問題。已建立 Tag 的 HACS 預發行版本仍為
`v0.5.0-beta.106`。兩者只支援匯入已由 Amaran Desktop 或 Sidus Link
建立的 Mesh，不支援新增燈具配對或建立新 Mesh。

[English documentation](README.md)

[beta.107 變更與驗證](docs/RELEASE-v0.5.0-beta.107.md) |
[v0.5.0-beta.106 發行說明](docs/RELEASE-v0.5.0-beta.106.md) |
[更新日誌](CHANGELOG.zh-TW.md)

GATT／CCCD 重連仍可能重試數分鐘；本次修正槽位追蹤問題，不保證固定時間
內完成連線。

## 已驗證燈具

| 燈具 | 色溫 | HS 色彩 | G/M 設定資料 |
| --- | --- | --- | --- |
| amaran T2c | 2500-7500 K | 支援 | 支援 |
| amaran 200x | 2700-6500 K | 不支援 | 不支援 |
| amaran 300c | 2500-7500 K | 支援 | 支援 |

實機環境為兩盞 T2c、一盞 200x 與一盞 300c，透過 ESPHome Bluetooth
Proxy 控制同一個既有 Sidus Mesh。

## 主要功能

- 開關與依燈具能力公開的 Home Assistant 標準燈光控制。
- 獨立亮度、100K 色溫、G/M、色調與飽和度數值控制。
- 多燈、多個動態 Mesh Proxy 候選，不寫死任何 MAC。
- 總開關開啟時由 HA 接管並維持一條 Mesh Proxy 連線。
- 總開關關閉時釋放 BLE，讓 Sidus Link 或 Amaran Desktop 恢復控制。
- active proxy 失聯後自動切換其他已匯入燈具。
- 只剩部分已匯入燈具通電時，依近期可達候選持續有界恢復；重新連線前會等待
  ESPHome GATT slot 安全釋放。
- 診斷資料會遮蔽 Mesh key，並將所有 MAC 轉為一致的匿名代號。

## HACS Custom Repository 安裝

本 beta 尚未加入 HACS 預設清單：

1. 開啟 HACS 的 **整合**。
2. 從選單進入 **自訂存放庫**。
3. 輸入 `https://github.com/Minwei21144/ha-amaran-ble`。
4. 類別選擇 **Integration**。
5. 下載 Amaran BLE，選擇 `v0.5.0-beta.106`。若看不到預發行版本，請先在
   HACS 對此 Repository 開啟 beta 版本追蹤。
6. 重新啟動 Home Assistant。

beta.106 的 HACS 發行包尚未包含 beta.107 修正。新 Release 建立前若要
測試，請從 `main` 手動複製 `custom_components/amaran_ble` 至 HA 的
`/config/custom_components/amaran_ble`，再重新啟動 Home Assistant。

Home Assistant 最低版本為 `2026.7.0`。

## 匯出既有 Mesh

在已安裝 Amaran Desktop 的 Windows 電腦執行：

```powershell
.\amaran_export_ha_config.cmd
```

也可以直接執行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\amaran_export_ha_config.ps1
```

腳本需要 Python，會讀取 Amaran Desktop 的本機資料庫與燈具能力檔，建立
`amaran-ble-ha-config.json`，並在終端機完整顯示 JSON，方便直接確認與複製。

輸出的 `net_key` 與 `app_key` 等同 Mesh 控制密碼。請勿將 JSON 貼到公開
Issue、Log、截圖或診斷附件。

## 匯入與控制

1. 到 **設定 > 裝置與服務 > 新增整合**。
2. 搜尋 **Amaran BLE**。
3. 貼上完整匯出 JSON。
4. 開啟 `Amaran BLE 總開關`。
5. 等待連線狀態成為 `connected` 後操作燈具。

## 數值控制

整合保留標準 `light` 實體，並依燈具能力建立以下 `number` 實體：

- **亮度：**每盞燈皆有，範圍為 `0.1%` 至 `100.0%`、步進 `0.1%`。使用
  獨立 Mesh brightness 命令，不會切換目前的 CCT／HSI 模式。
- **色溫：**依匯入燈具範圍，固定以 `100K` 為一步。
  固定白光燈具的 `min_kelvin` 等於 `max_kelvin`，不建立色溫控制；其燈光
  實體僅提供電源與亮度。舊版本留下的無效色溫實體會在整合載入時清除。
- **G/M：**`-10` 至 `+10`，只建立於 `supports_gm: true` 的燈具。
- **色調／飽和度：**色調 `0` 至 `360` 度、飽和度 `0%` 至 `100%`，只建立於
  `supports_hs: true` 的燈具。

調整單一數值時會保留其他色彩通道。HA 原生裝置頁會依目前語系的顯示名稱
排序，不採用整合建立實體的順序。

狀態意義：

- `released`：HA 已釋放 Mesh。
- `connecting`：正在掃描與嘗試候選燈具。
- `stabilizing`：Mesh 已登入，正在通過 30 秒可操作觀察期。
- `connected`：可以控制。
- `unavailable`：目前沒有可用候選。

連線持續 120 秒後才會在內部被標記為可信任代理；在觀察期內掉線的候選會
暫時隔離，避免一直重選短時間成功、稍後又斷線的燈具。

## 已知限制

- 不支援建立新 Mesh 或 provisioning。
- 首次接管通常需要數十秒；GATT 候選失敗時會更久。
- 非 active proxy 的燈具斷電後，約需 30-60 秒才可能顯示不可用，以避免
  BLE Mesh 正常丟包造成假離線。
- HA 原生裝置頁自行決定控制項順序；除非顯示名稱前綴或使用自訂卡片，
  整合無法強制指定畫面順序。
- 目前只有上表三種型號完成實機測試。
- 目前已完成 1/4 與 2/4 部分燈具通電接管測試；3/4、4/4 與長時間
  retention 壓力測試仍待補齊。
- 這是逆向研究的社群整合，燈具或官方軟體更新後可能需要跟著調整。

## Debug

若診斷顯示 `last_error_hint: bootstrap_gatt_profile_rejected`，代表通知設定
時預設的 GATT 位置表被拒絕，可能與該燈具的實際位置表不符。整合會優先對
同一候選執行完整服務探索；一般 CCCD 逾時與授權錯誤仍沿用既有恢復流程。
回報問題時請附上燈具型號及已檢查過的匿名診斷資料。

```yaml
logger:
  default: info
  logs:
    custom_components.amaran_ble: debug
```

分享診斷資料前仍應自行檢查內容。安全回報方式請見 [SECURITY.md](SECURITY.md)。

本專案採用 [MIT License](LICENSE)，與 Amaran、Aputure 或 Sidus Link 官方
沒有隸屬、背書或技術支援關係。
