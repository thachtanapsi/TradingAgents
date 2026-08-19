# Tích hợp social sentiment Việt Nam bằng FireAnt

## Trạng thái an toàn mặc định

GX profile dùng FireAnt cho `retail_social_signal` của mã Việt Nam. StockTwits
và Reddit bị tắt trong profile này; luồng upstream/quốc tế vẫn giữ hành vi cũ.
CafeF, VnExpress và các nguồn báo chí thuộc `media_tone`, không phải social.

Tính năng FireAnt mặc định bị khóa:

```dotenv
TRADINGAGENTS_VN_SOCIAL_PROVIDER=fireant
TRADINGAGENTS_FIREANT_AUTHORIZED=false
TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED=false
```

Chỉ đổi `TRADINGAGENTS_FIREANT_AUTHORIZED=true` sau khi APG có văn bản cho phép
tự động gọi API, lưu raw content/thông tin tác giả, tạo derived sentiment và giữ
dữ liệu theo chính sách này. Nếu dùng OpenAI hoặc LLM hosted khác, chỉ đổi thêm
`TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED=true` khi văn bản cũng cho phép đưa
nội dung vào hosted LLM. Hai cờ này là xác nhận vận hành, không thay thế hợp đồng.

FireAnt công bố API `GET /symbols/{symbol}/posts` với scope `posts-read`, nhưng
điều khoản công khai có giới hạn cá nhân/phi thương mại. Tham khảo [FireAnt API](https://api.fireant.vn/),
[điều khoản](https://corporate.fireant.vn/Home/TermsOfUse) và
[dịch vụ tổ chức](https://corporate.fireant.vn/home/services).

Khi bị khóa hoặc thiếu quyền, stage trả `unavailable`/`partial` tường minh. Nó
không gọi StockTwits/Reddit thay thế và không biến thiếu dữ liệu thành `neutral`.

## Cấu hình

Cài optional dependency, sau đó sao chép profile hosted hoặc Ollama:

```bash
python -m pip install -e ".[dev,gx-postgres,fireant]"
cp .env.hosted.example .env.hosted
# hoặc: cp .env.ollama.example .env.ollama
chmod 600 .env.hosted  # thay bằng .env.ollama nếu dùng profile local
```

Cấu hình trong file `.env` thật đã được Git ignore:

```dotenv
TRADINGAGENTS_VN_SOCIAL_PROVIDER=fireant
TRADINGAGENTS_FIREANT_AUTHORIZED=true
TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED=false

FIREANT_ACCESS_TOKEN=<bearer-token-co-scope-posts-read>
FIREANT_ARCHIVE_ENCRYPTION_KEY=<fernet-key>

TRADINGAGENTS_VN_SOCIAL_TICKERS=HPG,FPT,VCB
TRADINGAGENTS_VN_SOCIAL_LOOKBACK_DAYS=7
TRADINGAGENTS_VN_SOCIAL_MIN_POSTS=10
TRADINGAGENTS_VN_SOCIAL_MIN_UNIQUE_AUTHORS=5
TRADINGAGENTS_VN_SOCIAL_POLL_SECONDS=300
TRADINGAGENTS_SOCIAL_RAW_RETENTION_DAYS=90
#TRADINGAGENTS_SOCIAL_ARCHIVE_PATH=/secure/path/vn_social.sqlite3
```

Tạo encryption key một lần và lưu trong secret manager hoặc file `.env` mode
`0600`; mất key đồng nghĩa không thể giải mã archive cũ:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Không đặt token/key trong source code, argument dòng lệnh, `session.json` hoặc
commit Git. Đổi/thu hồi ngay credential từng xuất hiện trong chat hay log.

`FIREANT_ARCHIVE_ENCRYPTION_KEY` phải là Fernet key hợp lệ. Archive có key
verifier và sẽ từ chối mở nếu dùng nhầm key. Việc đổi key tại chỗ chưa được hỗ
trợ; khi cần rotation, tạo archive mới ở đường dẫn mới và áp dụng retention cho
archive cũ. Không xóa key khi vẫn cần đọc raw data chưa hết hạn.

## Kiểm tra và thu thập

`doctor` mặc định không gọi FireAnt hoặc LLM:

```bash
tradingagents-gx --env-file .env.hosted doctor
```

Khi chưa cấp quyền, kết quả đúng là:

```text
SKIP social: authorization locked
```

Sau khi đã có quyền/token/key/watchlist, kiểm tra live có chủ đích:

```bash
tradingagents-gx --env-file .env.hosted doctor --live-social
tradingagents-gx --env-file .env.hosted social status
```

`social status` vẫn chỉ đọc cấu hình/archive cục bộ. Chỉ `doctor --live-social`
và `social collect --once` gọi FireAnt; health check live chỉ đọc một trang để
giới hạn tải.

Thu thập một lần toàn watchlist hoặc một mã:

```bash
tradingagents-gx --env-file .env.hosted social collect --once
tradingagents-gx --env-file .env.hosted social collect --once --ticker HPG
```

Khi truyền `--ticker`, mã được thu thập trực tiếp và không cần nằm trong
`TRADINGAGENTS_VN_SOCIAL_TICKERS`. Watchlist chỉ áp dụng khi bỏ `--ticker`.

CLI cố ý chỉ hỗ trợ collector bounded `--once`. Dùng scheduler của hệ điều hành
để gọi nó mỗi 5 phút, bảo đảm chỉ có một instance tại một thời điểm. Ví dụ cron:

```cron
*/5 * * * * cd /path/to/TradingAgents && .venv/bin/tradingagents-gx --env-file .env.hosted social collect --once
```

Archive mặc định là `~/.tradingagents/cache/social/vn_social.sqlite3`, bật WAL,
ghi transaction atomic và đặt permission `0600`. Raw content và author fields
được mã hóa. Thiếu encryption key làm collector fail closed.

Archive hiện dùng schema v2. Ngoài identity/version/symbol/fetch run, schema này
lưu observation của engagement/provider sentiment theo thời điểm nhìn thấy,
snapshot claim và profile fingerprint. Archive v1 được migrate tại lần mở đầu
tiên và raw ciphertext được giữ nguyên. Trước khi nâng cấp, dừng collector rồi
tạo bản sao nhất quán bằng công cụ backup SQLite; mã hóa bản sao và áp dụng cùng
chính sách 90 ngày cho nó.

## Snapshot cuối phiên

Sau 15:15 `Asia/Ho_Chi_Minh` của một phiên GX đã hoàn tất, tạo snapshot bằng
đúng một quick-LLM run cho mỗi mã trong watchlist:

```bash
tradingagents-gx --env-file .env.ollama social snapshot \
  --date 2026-08-13 --live-llm
```

Với LLM hosted, phải bật riêng
`TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED=true`; nếu không, CLI dừng trước khi
gọi LLM. Lệnh kiểm tra phiên hoàn tất qua GX, dùng Sentiment stage hiện hữu và
lưu snapshot idempotent. Snapshot đã hoàn tất với cùng social profile, model và
input fingerprint được bỏ qua trước khi gọi LLM; dữ liệu cũ không bị overwrite.

Lệnh `snapshot` không gọi FireAnt hoặc RSS báo chí: hai evidence lane chỉ được
đọc từ archive. Nó vẫn kiểm tra phiên qua GX. `--live-llm` luôn bắt buộc nên chạy
lệnh này đồng nghĩa cho phép đúng một quick-LLM invocation cho mỗi mã chưa có
snapshot tương thích. Nếu mọi evidence lane đều `unavailable`, stage không gọi
LLM. Xem cấu hình CafeF/VnExpress tại
[vietnam-editorial-media.md](vietnam-editorial-media.md).

### Local và hosted được phân loại thế nào

Với social content, chỉ `ollama` hoặc `openai_compatible` trỏ tới `localhost`,
`127.0.0.0/8` hay `::1` mới được coi là local. Ollama ở IP LAN, hostname từ xa,
cloud tunnel hoặc `host.docker.internal` được coi là hosted và cần
`TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED=true`. Các provider khác như
`openai`, `anthropic` hay `google` luôn được coi là hosted. Cờ này chỉ được bật
khi phạm vi văn bản FireAnt/APG cho phép gửi nội dung tới backend đó.

Historical analysis chỉ dùng post thỏa cả `published_at <= as_of` và
`first_seen_at <= as_of`. Backfill hôm nay không được coi là dữ liệu đã biết
trong quá khứ. Sau khi raw hết hạn, pipeline tái dùng aggregate snapshot thay vì
chạy lại hay suy diễn.

Do FireAnt chỉ công bố pagination offset/limit, collector không thể chứng minh
tuyệt đối thứ tự và độ đầy đủ lịch sử ở phía provider. Vì vậy ngay cả cửa sổ thu
thập đều đặn cũng mang `point_in_time_quality=proxy`; polling gap, pagination bị
chặn hoặc ordering bất thường hạ chất lượng xuống `partial`. `first_seen_at`
không bao giờ được sửa để tránh look-ahead.

## Retention và session

Purge raw content/thông tin tác giả quá 90 ngày:

```bash
tradingagents-gx --env-file .env.hosted social purge
```

Aggregate snapshot không định danh được giữ lại. `session.json` schema v4 chỉ
lưu provider/status, sample size, unique authors, cửa sổ, fetch/snapshot ID,
point-in-time quality và warnings; không lưu raw post, author, token hay key.
`social_profile` là immutable. Session v1 được migrate thành social
`provider=legacy`; session v1/v2 nhận media profile `legacy`. Muốn chuyển sang
FireAnt hoặc thay media archive/prompt/alias policy phải tạo run mới. Sentiment
fingerprint và FireAnt daily snapshot đều bind `media_profile_fingerprint`, nên
snapshot cũ không được tái dùng với cấu hình RSS khác.

`social purge` xóa các version/identity và fetch audit đã hết hạn, bật SQLite
`secure_delete`, checkpoint/truncate WAL và chạy `VACUUM`; snapshot aggregate vẫn
được giữ. Đây là best effort trên file đang quản lý, không thể xóa dữ liệu từng
được sao chép vào Time Machine/APFS snapshot, backup, log, filesystem snapshot
hoặc block SSD đã wear-level. Hãy đặt archive và mọi backup dưới cùng retention,
mã hóa backup, giới hạn quyền đọc và xóa bản sao hết hạn riêng. Snapshot có bộ
lọc bỏ field raw/identity theo cấu trúc, nhưng không phải hệ thống DLP tổng quát;
không đưa trích dẫn hay danh tính vào narrative/custom prompt.

Coverage mặc định:

- `available`: ít nhất 10 post, 5 tác giả và archive phủ đủ 7 ngày.
- `partial`: còn evidence dùng được nhưng coverage/pagination/archive có gap.
- `unavailable`: khóa quyền, thiếu key/token/mẫu hoặc lỗi 401/403/429/timeout.
- `neutral`: chỉ khi có dữ liệu thật và ý kiến cân bằng.

`unavailable` luôn có band/score/confidence bằng `null`; không được map về điểm
0. `partial` có thể có score nếu còn evidence thật và phải mang cảnh báo coverage.
Overall là `available` khi mọi lane đã bật đều đầy đủ, `partial` khi còn ít nhất
một lane dùng được nhưng lane khác thiếu/gap, và chỉ là `unavailable` khi cả
`retail_social_signal` lẫn `media_tone` đều không dùng được.
