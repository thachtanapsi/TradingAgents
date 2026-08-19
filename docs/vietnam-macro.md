# Dữ liệu vĩ mô Việt Nam từ NSO và SBV

GX/Vietnam profile dùng lane `vn_macro` cho News Analyst. Lane này thu thập dữ
liệu công khai chính thức từ NSO/NSDP và SBV, chuẩn hóa vào SQLite rồi chỉ đọc
archive khi phân tích. `FRED_API_KEY` không được dùng trong GX News; FRED vẫn giữ
nguyên cho profile upstream/quốc tế.

## Cài đặt và cấu hình

```bash
cd /Users/thachtan/Documents/source/APG/TradingAgents
source .venv/bin/activate
python -m pip install -e ".[dev,gx-postgres,fireant,vn-media,vn-macro]"
```

Thêm vào file môi trường GX của bạn:

```dotenv
TRADINGAGENTS_VN_MACRO_ENABLED=true
TRADINGAGENTS_VN_MACRO_PROVIDERS=nso_sdmx,nso_release,sbv_html
TRADINGAGENTS_VN_MACRO_LOOKBACK_MONTHS=24
TRADINGAGENTS_VN_MACRO_STRICT_PIT=true
TRADINGAGENTS_VN_MACRO_TIMEOUT_SECONDS=15
#TRADINGAGENTS_VN_MACRO_ARCHIVE_PATH=/absolute/path/to/vn_macro.sqlite3
```

Archive mặc định là
`~/.tradingagents/cache/macro/vn_macro.sqlite3`. Nó chỉ lưu số liệu công khai đã
chuẩn hóa và provenance, không lưu raw XML/Excel/HTML và không cần encryption
key.

## Thu thập và kiểm tra

Chạy một vòng thu thập tất cả nguồn:

```bash
tradingagents-gx --env-file .env.postgres-hosted macro collect --once
```

Có thể giới hạn theo nhóm hoặc adapter:

```bash
tradingagents-gx --env-file .env.postgres-hosted macro collect --once --source nso
tradingagents-gx --env-file .env.postgres-hosted macro collect --once --source sbv
tradingagents-gx --env-file .env.postgres-hosted macro collect --once --source nso_sdmx
```

Các lệnh kiểm tra không gọi LLM:

```bash
tradingagents-gx --env-file .env.postgres-hosted macro status
tradingagents-gx --env-file .env.postgres-hosted macro show \
  --as-of 2026-08-18 --json

# Mặc định doctor chỉ đọc archive/config. Cờ này mới cho phép gọi NSO/SBV.
tradingagents-gx --env-file .env.postgres-hosted doctor --live-macro
```

Sau khi archive có dữ liệu, chạy News hoặc full pipeline như bình thường:

```bash
tradingagents-gx --env-file .env.postgres-hosted stage news \
  --ticker VIC --date 2026-08-18 --analysts news

tradingagents-gx --env-file .env.postgres-hosted full \
  --ticker VIC --date 2026-08-18 \
  --analysts market,sentiment,news,fundamentals
```

`session.json` schema v6 lưu `macro_profile` immutable, định danh
`analysis_mode`/`analysis_cutoff` và metadata aggregate
(`status`, nguồn, kỳ, fetch ID, PIT quality, stale/warnings), không lưu raw
response hoặc archive path. Session v1-v4 được migrate thành macro profile
`legacy`; không resume session legacy dưới profile `vn_macro`, hãy tạo run mới.

## Nguồn và semantics

- NSDP SDMX/Excel: CPI, core CPI, GDP, IIP và xuất nhập khẩu.
- Báo cáo kinh tế–xã hội NSO: bán lẻ và headline/đối chiếu kỳ công bố.
- SBV: tỷ giá trung tâm, lãi suất điều hành, lãi suất liên ngân hàng và tăng
  trưởng tín dụng. Tăng trưởng tín dụng được phép dùng báo cáo NSO có dẫn số
  liệu SBV khi trang thống kê SBV không truy cập được; provenance vẫn ghi NSO.
- `available`: bundle đủ và không stale; `partial`: còn dữ liệu dùng được nhưng
  thiếu nguồn/stale; `unavailable`: không còn observation hợp lệ.
- Missing luôn là `null`, không đổi thành `0`. Dữ liệu SBV bị chặn/WAF không được
  thay bằng báo chí hay số liệu suy đoán.

Historical query chỉ nhận version thỏa cả `published_at <= as_of` và
`first_seen_at <= as_of`. Revision mới không thay đổi một historical run cũ;
backfill tải hôm nay không được trình bày như dữ liệu đã biết trong quá khứ.

## Lịch chạy gợi ý

Không cần giữ một process chạy liên tục. Chạy collector lúc 08:15 và 16:30 theo
`Asia/Ho_Chi_Minh`. Ví dụ cron trên máy đã đặt timezone Việt Nam:

```cron
15 8  * * * cd /path/to/TradingAgents && .venv/bin/tradingagents-gx --env-file .env.postgres-hosted macro collect --once
30 16 * * * cd /path/to/TradingAgents && .venv/bin/tradingagents-gx --env-file .env.postgres-hosted macro collect --once
```

Đặt quyền file `.env.postgres-hosted` là `0600`, dùng đường dẫn tuyệt đối trong
cron/launchd và theo dõi exit code. Collector có transaction atomic nên chạy lại
một vòng bị gián đoạn không làm hỏng observation đã hoàn tất.

Trên macOS có thể sao chép
[launchd template](com.tradingagents.vn-macro.plist.example) vào
`~/Library/LaunchAgents/com.tradingagents.vn-macro.plist`, thay toàn bộ
`/ABSOLUTE/PATH/TO/TradingAgents`, rồi kiểm tra XML bằng `plutil -lint` trước khi
`launchctl bootstrap`. `StartCalendarInterval` dùng timezone hiện tại của máy;
hãy đặt macOS ở `Asia/Ho_Chi_Minh` nếu cần đúng hai mốc trên.

## Nguồn chính thức

- [NSDP Việt Nam](https://nsdp.nso.gov.vn/)
- [Báo cáo kinh tế–xã hội hàng tháng của NSO](https://www.nso.gov.vn/bao-cao-tinh-hinh-kinh-te-xa-hoi-hang-thang/)
- [Bảng lãi suất SBV](https://sbv.gov.vn/vi/l%C3%A3i-su%E1%BA%A5t1)
- [Bảng tỷ giá SBV](https://sbv.gov.vn/vi/t%E1%BB%B7-gi%C3%A1)

Khi trình bày hoặc phân phối lại số liệu, giữ attribution NSO/SBV và để APG xác
nhận điều kiện sử dụng cho môi trường thương mại.
