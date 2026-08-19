# Tích hợp tin Việt Nam bằng RSS CafeF và VnExpress

## Vai trò của từng nguồn

GX profile tách ba loại evidence:

- GX corporate events là `official_disclosures`.
- CafeF và VnExpress là `editorial_media`, dùng cho News Analyst và
  `media_tone` của Sentiment Analyst.
- FireAnt là `retail_social_signal`, không bị trộn với báo chí.

Adapter chỉ tải RSS chính thức. Nó không mở trang bài viết, không crawl toàn
văn, hình ảnh, bình luận hay nội dung phía sau đăng nhập. Các feed cố định gồm:

```text
https://cafef.vn/thi-truong-chung-khoan.rss
https://cafef.vn/doanh-nghiep.rss
https://cafef.vn/tai-chinh-ngan-hang.rss
https://cafef.vn/vi-mo-dau-tu.rss
https://vnexpress.net/rss/kinh-doanh.rss
```

Khi hiển thị hoặc sử dụng kết quả, luôn giữ tên nguồn và canonical URL. CafeF
yêu cầu ghi nguồn khi sử dụng RSS. VnExpress công bố RSS miễn phí cho cá nhân và
tổ chức phi lợi nhuận; APG phải xác nhận phạm vi thương mại bằng văn bản trước
khi bật. Xem [CafeF RSS](https://cafef.vn/index.rss),
[VnExpress RSS](https://vnexpress.net/rss) và
[điều khoản VnExpress](https://vnexpress.net/dieu-khoan-su-dung).

## Cài đặt và cấu hình khóa mặc định

```bash
cd /Users/thachtan/Documents/source/APG/TradingAgents
source .venv/bin/activate
python -m pip install -e ".[dev,gx-postgres,fireant,vn-media]"
```

Sao chép `.env.hosted.example` hoặc `.env.ollama.example` thành file riêng đã
được Git ignore. Cấu hình ban đầu phải giữ cả hai nguồn ở trạng thái khóa:

```dotenv
TRADINGAGENTS_VN_MEDIA_PROVIDERS=cafef_rss,vnexpress_rss
TRADINGAGENTS_CAFEF_RSS_AUTHORIZED=false
TRADINGAGENTS_CAFEF_HOSTED_LLM_AUTHORIZED=false
TRADINGAGENTS_VNEXPRESS_RSS_AUTHORIZED=false
TRADINGAGENTS_VNEXPRESS_HOSTED_LLM_AUTHORIZED=false

VN_MEDIA_ARCHIVE_ENCRYPTION_KEY=
TRADINGAGENTS_VN_MEDIA_TICKERS=HPG,FPT,VCB
TRADINGAGENTS_VN_MEDIA_LOOKBACK_DAYS=7
TRADINGAGENTS_VN_MEDIA_MIN_ARTICLES=3
TRADINGAGENTS_VN_MEDIA_POLL_SECONDS=300
TRADINGAGENTS_VN_MEDIA_RAW_RETENTION_DAYS=30
#TRADINGAGENTS_VN_MEDIA_ARCHIVE_PATH=/secure/path/vn_media.sqlite3
```

Chỉ bật cờ `*_RSS_AUTHORIZED=true` cho nguồn đã được phê duyệt. Nếu quick LLM
là OpenAI, provider hosted khác, Ollama từ xa hoặc OpenAI-compatible từ xa, phải
có thêm phê duyệt đưa RSS title/summary tới backend đó rồi mới bật cờ
`*_HOSTED_LLM_AUTHORIZED=true`. Loopback Ollama/OpenAI-compatible được coi là
local. Các cờ authorization được đọc trực tiếp từ environment; chúng không nằm
trong config hoặc `session.json`.

Tạo Fernet key một lần, lưu trong secret manager hoặc file env mode `0600`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Không commit key. Sai hoặc thiếu key làm archive fail closed.

## Kiểm tra, collect và purge

`doctor` mặc định không gọi RSS hoặc LLM:

```bash
tradingagents-gx --env-file .env.hosted doctor
tradingagents-gx --env-file .env.hosted media status
```

Khi mọi nguồn bị khóa, kết quả đúng là `SKIP media: authorization locked`.
Sau khi bật ít nhất một nguồn, kiểm tra live có chủ đích:

```bash
tradingagents-gx --env-file .env.hosted doctor --live-media
```

Collect toàn watchlist hoặc một mã:

```bash
tradingagents-gx --env-file .env.hosted media collect --once
tradingagents-gx --env-file .env.hosted media collect --once --ticker HPG
```

Khi truyền `--ticker`, mã không cần có sẵn trong
`TRADINGAGENTS_VN_MEDIA_TICKERS`; watchlist chỉ được dùng khi bỏ `--ticker`.
Sau khi thay đổi alias/matching policy, collect lại mã để tạo mapping theo policy
mới. Dữ liệu được thấy lần đầu sau 15:00 không được dùng ngược cho run close
`--date` cùng ngày và báo cáo/session cũ không tự cập nhật. Run mới
`--as-of-now` có cutoff muộn hơn vẫn có thể dùng dữ liệu đó theo strict PIT.

Để collect media và FireAnt cho một mã rồi chạy live với cùng một cutoff
immutable, có thể dùng shorthand của run mới:

```bash
tradingagents-gx --env-file .env.hosted full \
  --ticker VIC --as-of-now --collect-evidence
```

Shorthand này gọi media trước, FireAnt sau; một lane lỗi chỉ sinh cảnh báo đã
redact. Sau hai attempt, CLI mới lấy giờ `Asia/Ho_Chi_Minh` đúng một lần và tạo
session live. Nó không collect macro và không gọi LLM trong pha collect.
`--collect-evidence` không được dùng với `--date` hoặc `--session`.

CLI chỉ chạy một pass bounded. Dùng cron/launchd gọi mỗi 5 phút và chỉ cho phép
một instance collector tại một thời điểm:

```cron
*/5 * * * * cd /path/to/TradingAgents && .venv/bin/tradingagents-gx --env-file .env.hosted media collect --once
```

Xóa raw title/summary/URL quá 30 ngày:

```bash
tradingagents-gx --env-file .env.hosted media purge
```

Archive mặc định là `~/.tradingagents/cache/media/vn_media.sqlite3`, dùng WAL,
transaction atomic, thư mục `0700` và file `0600`. Raw RSS được mã hóa; aggregate
coverage không chứa nội dung có thể được giữ lại. Retention trên archive không
tự xóa Time Machine, APFS snapshot hay bản backup khác; mọi bản sao phải tuân
cùng chính sách.

## Point-in-time và trạng thái evidence

Historical analysis chỉ nhận article thỏa đồng thời:

```text
published_at <= as_of
first_seen_at <= as_of
```

Date-only analysis dùng cutoff `15:00:00+07:00`. Bài backfill hôm nay không được
coi là evidence đã biết trong quá khứ. Vì RSS không chứng minh được lịch sử đầy
đủ tuyệt đối, chất lượng tốt nhất là `proxy`; collection gap lớn hơn ba chu kỳ
polling hạ xuống `partial`.

- Không có article phù hợp: `unavailable`, không phải `neutral`.
- Một hoặc hai article: `partial`.
- Từ ba article, coverage đủ và mọi nguồn đã bật đều khỏe: `available`.
- Nguồn chủ động khóa không làm hạ nguồn đang hoạt động; nguồn đã bật nhưng lỗi
  làm kết quả `partial` nếu vẫn còn evidence khác.

Ticker matching dùng ticker và alias doanh nghiệp lấy từ GX. Một bare ticker
ngắn chỉ được chấp nhận khi có tên doanh nghiệp hoặc ngữ cảnh chứng khoán. Alias
GX là current-state nên historical match được đánh dấu `proxy`.

## Session và chạy pipeline

GX profile cấu hình riêng:

```text
get_disclosures   -> gx_market_info
get_editorial_news -> vn_media (CafeF + VnExpress fan-in)
```

Hai nguồn được tổng hợp song song, không fallback thay thế nhau. Session schema
v3 giữ `media_profile` immutable gồm provider list, lookback, threshold, archive
ID, schema, alias policy và prompt version. Nó không lưu archive path,
authorization, encryption key, title hoặc summary. Đổi bất kỳ identity field
nào phải tạo run mới; v1/v2 được đọc thành media profile `legacy`.

Sentiment và News input fingerprint đều chứa media profile. FireAnt daily
snapshot cũng được bind bằng `media_profile_fingerprint`, nên snapshot tạo với
archive/prompt/alias cũ không được tái dùng cho profile mới. Session chỉ giữ
status, sample count, time window, fetch ID, PIT quality và warnings.

Sau khi collector đã có dữ liệu, chạy như bình thường:

```bash
tradingagents-gx --env-file .env.hosted stage sentiment \
  --ticker HPG --analysis-date 2026-08-13

tradingagents-gx --env-file .env.hosted stage news \
  --session ~/.tradingagents/runs/HPG/2026-08-13/<run-id>/session.json
```

Nguồn bị khóa hoặc archive chưa có coverage không kích hoạt crawl trực tiếp từ
stage. Analyst báo `partial/unavailable` tường minh và không tạo neutral giả.
