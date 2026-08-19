# Cách chạy TradingAgents GX cho mã cổ phiếu khác

Hướng dẫn này áp dụng cho cổ phiếu thường trên HOSE, HNX và UPCOM, ví dụ
`FPT`, `VCB`, `SSI`, `MBS` hoặc `BSR`.

Sử dụng mã nội bộ viết hoa và không thêm hậu tố `.VN`.

## 1. Chuẩn bị môi trường Python

```bash
cd /Users/thachtan/Documents/source/APG/TradingAgents
source .venv/bin/activate
```

Nếu chưa cài dependency:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,gx-postgres,vn-macro]"
```

Nếu đã được cấp quyền và muốn bật cả social archive FireAnt lẫn RSS CafeF /
VnExpress, cài đầy đủ hai optional extra:

```bash
python -m pip install -e ".[dev,gx-postgres,fireant,vn-media,vn-macro]"
```

Nếu chỉ bật một lane, có thể chỉ thêm `fireant` hoặc `vn-media` tương ứng. Không
cần hai extra mã hóa này khi cả FireAnt và RSS vẫn bị khóa. `vn-macro` dùng cho
NSO/SBV và không cần encryption key.

## 2. Tạo profile PostgreSQL và OpenAI

```bash
cp .env.hosted .env.postgres-hosted
chmod 600 .env.postgres-hosted
```

Mở `.env.postgres-hosted` bằng editor và cấu hình:

```dotenv
OPENAI_API_KEY=<openai-api-key>

TRADINGAGENTS_QUICK_LLM_PROVIDER=openai
TRADINGAGENTS_QUICK_THINK_LLM=gpt-5.4-mini
TRADINGAGENTS_QUICK_LLM_BASE_URL=https://api.openai.com/v1
#TRADINGAGENTS_QUICK_LLM_API_KEY=
TRADINGAGENTS_DEEP_LLM_PROVIDER=openai
TRADINGAGENTS_DEEP_THINK_LLM=gpt-5.5
TRADINGAGENTS_DEEP_LLM_BASE_URL=https://api.openai.com/v1
#TRADINGAGENTS_DEEP_LLM_API_KEY=
TRADINGAGENTS_OPENAI_REASONING_EFFORT=medium
TRADINGAGENTS_OUTPUT_LANGUAGE=Vietnamese
TRADINGAGENTS_MAX_DEBATE_ROUNDS=1
TRADINGAGENTS_MAX_RISK_ROUNDS=1
TRADINGAGENTS_CHECKPOINT_ENABLED=true
TRADINGAGENTS_LLM_MAX_RETRIES=6

GX_DATA_TRANSPORT=postgres
GX_MARKET_INFO_DATABASE_URL=postgresql://<readonly-user>@<db-host>:<port>/g_market_info_1229
PGPASSWORD='<database-password>'
GX_MARKET_INFO_EXPECTED_DB=g_market_info_1229
GX_DATA_TIMEOUT_SECONDS=10

TRADINGAGENTS_VN_MACRO_ENABLED=true
TRADINGAGENTS_VN_MACRO_PROVIDERS=nso_sdmx,nso_release,sbv_html
TRADINGAGENTS_VN_MACRO_LOOKBACK_MONTHS=24
TRADINGAGENTS_VN_MACRO_STRICT_PIT=true
TRADINGAGENTS_VN_MACRO_TIMEOUT_SECONDS=15
```

Hai API key theo role là tùy chọn. Nếu để trống, cả hai profile tái sử dụng
`OPENAI_API_KEY`. Có thể đổi riêng provider/base URL của từng role; chẳng hạn
Quick chạy Ollama local và Deep chạy OpenAI. Hai biến shared cũ
`TRADINGAGENTS_LLM_PROVIDER`/`TRADINGAGENTS_LLM_BACKEND_URL` vẫn được hỗ trợ làm
fallback nhưng không cần thêm vào cấu hình mới.

Nên sử dụng một PostgreSQL role riêng chỉ có quyền `CONNECT` và `SELECT`.
Để password trong `PGPASSWORD` thay vì nhúng vào URL, nhất là khi password có ký
tự đặc biệt. Các file `.env.*` thật đã được Git ignore; không force-add chúng
vào repository.

### Dữ liệu cash-flow theo từng transport

Khi dùng `GX_DATA_TRANSPORT=postgres`, Fundamentals Analyst đọc trực tiếp báo
cáo lưu chuyển tiền tệ từ bảng `public.fiin_cashflow`, tương tự income statement
và balance sheet. Truy vấn lịch sử chỉ nhận các bản ghi:

- Có `status = 1`.
- Có `publicdate <= as_of` và `createdate <= as_of`.
- Có `updateddate` rỗng hoặc `updateddate <= as_of`.
- Thuộc `lengthreport` phù hợp với `quarterly` hoặc `annual`.

`quarterly` chỉ lấy mã `1`–`4`, `annual` chỉ lấy mã `5`, và `limit` được giới
hạn từ `1` đến `20`. Hai kỳ lũy kế `6` và `9` không bị trộn với quý độc lập
trong v1; dòng `status = 2` cũng không được dùng để tự dựng lại revision cũ.

Các revision hợp lệ được khử trùng theo `(yearreport, lengthreport)` và giữ bản
công bố/cập nhật mới nhất không vượt quá `as_of`. Nếu revision lịch sử đã bị ghi
đè trong nguồn và không thể khôi phục, chất lượng point-in-time vẫn được đánh
dấu `partial`. Giá trị thiếu được giữ là `null`, không chuyển thành `0`.

Với công ty thường (`comtypecode=CT`), các trường cash-flow trong phần
`normalized` tuân theo đúng **data dictionary Navisoft ngày 2024-12-26**; hệ
thống không tự suy đoán ý nghĩa các cột `cfa*`. Mỗi kỳ vẫn giữ các trường nguồn
và metadata như kỳ báo cáo, ngày công bố, trạng thái kiểm toán và phương pháp
lập báo cáo.

| Trường chuẩn hóa | Trường nguồn Navisoft |
| --- | --- |
| `net_cash_from_operating_activities` | `cfa18` |
| `capital_expenditures` | `cfa19` |
| `net_cash_from_investing_activities` | `cfa26` |
| `net_cash_from_financing_activities` | `cfa34` |
| `net_change_in_cash` | `cfa35` |
| `cash_and_cash_equivalents_beginning` | `cfa36` |
| `foreign_exchange_effect` | `cfa37` |
| `cash_and_cash_equivalents_ending` | `cfa38` |
| `free_cash_flow` | `cfa18 + cfa19` |

`capital_expenditures` giữ nguyên dấu của dữ liệu nguồn; khoản chi mua sắm âm
không bị đổi thành số dương. `free_cash_flow` chỉ được tính khi cả `cfa18` và
`cfa19` đều có dữ liệu, theo công thức `cfa18 + cfa19`; nếu thiếu một vế thì giữ
`null`. Mapping CT này không áp dụng cho ngân hàng (`NH`), công ty chứng khoán
(`CK`) hoặc bảo hiểm (`BH`) khi chưa có dictionary chính thức tương ứng. Adapter
không tự suy đoán đơn vị tiền tệ từ workbook.

Transport `api` hiện chưa có cash-flow ở GX Analysis API nên sẽ trả
`NOT_MODELED`/`unavailable` cho công cụ này cho tới khi backend được bổ sung.
GX profile không còn fallback cash-flow sang Yahoo: nếu cần cash-flow, phải chọn
`GX_DATA_TRANSPORT=postgres`; lỗi DB hoặc thiếu dữ liệu sẽ được báo rõ thay vì
âm thầm trộn dữ liệu Yahoo vào phiên phân tích GX.

## 3. Chạy kiểm tra trước

```bash
tradingagents-gx --env-file .env.postgres-hosted doctor
```

Kết quả bình thường có dạng:

```text
OK runs_dir: ...
OK llm: OPENAI_API_KEY
OK gx: postgres; last session YYYY-MM-DD
```

`doctor` không gọi LLM có phí và không gọi NSO/SBV nếu thiếu `--live-macro`.
Thu thập macro lần đầu trước khi chạy News:

```bash
tradingagents-gx --env-file .env.postgres-hosted macro collect --once
tradingagents-gx --env-file .env.postgres-hosted macro status
```

Chỉ chạy full pipeline khi các mục bắt buộc đều `OK`.
Ngày phân tích nên là ngày phiên hoàn tất được `doctor` trả về hoặc một phiên
trước đó.

## 4. Chạy full pipeline

Ví dụ phân tích `FPT`:

```bash
tradingagents-gx --env-file .env.postgres-hosted full \
  --ticker FPT \
  --date 2026-08-12
```

Ví dụ phân tích `VCB`:

```bash
tradingagents-gx --env-file .env.postgres-hosted full \
  --ticker VCB \
  --date 2026-08-12
```

Mỗi run mới bắt buộc chọn đúng một chế độ thời gian:

- `--date YYYY-MM-DD`: phân tích tại cuối phiên 15:00 của ngày đã chọn.
- `--as-of-now`: phân tích live tại một cutoff hiện tại, có timezone
  `Asia/Ho_Chi_Minh`, được đóng băng một lần và lưu trong session.

Ví dụ phân tích `VIC` bằng dữ liệu đang có trong archive tại thời điểm chạy:

```bash
tradingagents-gx --env-file .env.postgres-hosted full \
  --ticker VIC \
  --as-of-now
```

Muốn thu thập một pass CafeF/VnExpress rồi FireAnt cho đúng mã trước khi đóng
băng cutoff, thêm `--collect-evidence`:

```bash
tradingagents-gx --env-file .env.postgres-hosted full \
  --ticker VIC \
  --as-of-now \
  --collect-evidence
```

Hai collector chạy tuần tự và độc lập. Lỗi của một nguồn được redact, in thành
cảnh báo rồi nguồn còn lại vẫn tiếp tục. Tùy chọn này không collect NSO/SBV,
không gọi macro và không gọi LLM trong pha collect. Macro vẫn dùng archive được
quản lý riêng bằng `macro collect --once`. Nếu không truyền
`--collect-evidence`, cutoff được lấy ngay trước khi tạo session.

Pipeline lần lượt chạy:

```text
Market → Sentiment → News → Fundamentals
       → Bull/Bear Research → Research Manager
       → Trader → Risk Analysts → Portfolio Manager
```

Khi hoàn thành, CLI in đường dẫn session:

```text
~/.tradingagents/runs/FPT/2026-08-12/<run-id>/session.json
```

## 5. Xem kết quả

Xem toàn bộ báo cáo và trạng thái:

```bash
tradingagents-gx show \
  ~/.tradingagents/runs/FPT/2026-08-12/<run-id>/session.json
```

Chỉ xem quyết định cuối:

```bash
jq -r '.state.final_trade_decision' \
  ~/.tradingagents/runs/FPT/2026-08-12/<run-id>/session.json
```

Xem riêng từng báo cáo:

```bash
jq -r '.state.market_report' <session.json>
jq -r '.state.sentiment_report' <session.json>
jq -r '.state.news_report' <session.json>
jq -r '.state.fundamentals_report' <session.json>
jq -r '.state.investment_plan' <session.json>
jq -r '.state.trader_investment_plan' <session.json>
jq -r '.state.final_trade_decision' <session.json>
```

## 6. Chạy từng stage

Khởi tạo session bằng Market Analyst:

```bash
tradingagents-gx --env-file .env.postgres-hosted stage market \
  --ticker FPT \
  --analysis-date 2026-08-12
```

Hoặc tạo một live session mới và collect evidence trước khi chạy stage đầu:

```bash
tradingagents-gx --env-file .env.postgres-hosted stage news \
  --ticker VIC \
  --as-of-now \
  --collect-evidence \
  --analysts news
```

Sao chép đường dẫn session được CLI in ra:

```bash
SESSION="/absolute/path/to/session.json"
```

Chạy các analyst còn lại:

```bash
tradingagents-gx --env-file .env.postgres-hosted stage sentiment \
  --session "$SESSION"

tradingagents-gx --env-file .env.postgres-hosted stage news \
  --session "$SESSION"

tradingagents-gx --env-file .env.postgres-hosted stage fundamentals \
  --session "$SESSION"
```

Chạy Research, Trader và Risk:

```bash
tradingagents-gx --env-file .env.postgres-hosted stage research \
  --session "$SESSION"

tradingagents-gx --env-file .env.postgres-hosted stage trader \
  --session "$SESSION"

tradingagents-gx --env-file .env.postgres-hosted stage risk \
  --session "$SESSION"
```

Quy tắc resume:

- `research` cần ít nhất một analyst report đã hoàn tất và không rỗng.
- `trader` cần `research` hoàn tất.
- `risk` cần `trader` hoàn tất.
- Chạy lại analyst sẽ tự xóa kết quả Research, Trader và Risk cũ.
- Chạy lại Research sẽ xóa Trader và Risk.
- Chạy lại Trader sẽ xóa Risk.
- Khi resume phải dùng cùng ticker, ngày, transport, LLM provider và model.
- Lệnh có `--session` không được kèm `--date`, `--as-of-now` hoặc
  `--collect-evidence`; session luôn tiếp tục với cutoff immutable đã lưu.
- `--collect-evidence` chỉ hợp lệ khi tạo run mới bằng `--as-of-now`; không dùng
  với `--date` hoặc khi resume.

## 7. Chạy bằng Ollama local

Khởi động Ollama và tải model:

```bash
ollama serve
ollama pull qwen3:8b
```

Tạo profile:

```bash
cp .env.ollama .env.postgres-ollama
chmod 600 .env.postgres-ollama
```

Trong `.env.postgres-ollama`, giữ cấu hình Ollama và thêm:

```dotenv
GX_DATA_TRANSPORT=postgres
GX_MARKET_INFO_DATABASE_URL=postgresql://<readonly-user>@<db-host>:<port>/g_market_info_1229
PGPASSWORD='<database-password>'
GX_MARKET_INFO_EXPECTED_DB=g_market_info_1229
GX_DATA_TIMEOUT_SECONDS=10
```

Kiểm tra và chạy:

```bash
tradingagents-gx --env-file .env.postgres-ollama doctor

tradingagents-gx --env-file .env.postgres-ollama full \
  --ticker SSI \
  --date 2026-08-12
```

Ollama local không phát sinh chi phí OpenAI, nhưng Yahoo global news,
CafeF/VnExpress, FireAnt, NSO/SBV và Polymarket vẫn có thể cần Internet. GX News
không gọi FRED; profile upstream/quốc tế mới giữ đường FRED.

## 8. Xử lý lỗi thường gặp

### `doctor` báo lỗi PostgreSQL

Kiểm tra:

- VPN hoặc kết nối tới mạng nội bộ.
- Host và port database.
- Username/password.
- Database phải đúng `g_market_info_1229`.
- Role phải có quyền `CONNECT` và `SELECT`.

### `ModuleNotFoundError: No module named 'cli'` trên macOS

Trước hết, bảo đảm cả Python và lệnh `tradingagents-gx` đều thuộc virtualenv của
repository hiện tại:

```bash
cd /Users/thachtan/Documents/source/APG/TradingAgents
source .venv/bin/activate

which python
which tradingagents-gx
python -m pip show tradingagents
```

Hai lệnh `which` phải trỏ lần lượt tới `.venv/bin/python` và
`.venv/bin/tradingagents-gx`. Sau đó xóa cờ `hidden` có thể bị kế thừa trên
macOS, cài lại editable package và làm mới command cache của Zsh:

```bash
chflags -R nohidden .venv
python -m pip install --force-reinstall --no-deps -e ".[dev,gx-postgres]"
rehash

python -c "import cli; print(cli.__file__)"
tradingagents-gx --help
```

Lệnh kiểm tra `import cli` phải in đường dẫn
`.../TradingAgents/cli/__init__.py`. Nếu nó vẫn báo lỗi sau khi đã cài package,
kiểm tra Python thực sự đang đọc site-packages nào:

```bash
python -c "import sys; print(sys.executable); print(*sys.path, sep='\n')"
python -c "import importlib.util; print(importlib.util.find_spec('cli'))"
ls -lO .venv
```

Nếu `find_spec('cli')` vẫn trả `None`, tạo một virtualenv sạch để loại trừ file
`.pth` hoặc metadata editable install bị hỏng. Giữ virtualenv cũ dưới tên
`.venv.broken` để có thể kiểm tra lại, không xóa ngay:

```bash
deactivate
mv .venv .venv.broken

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,gx-postgres,fireant,vn-media,vn-macro]"

python -c "import cli; print(cli.__file__)"
tradingagents-gx --env-file .env.postgres-hosted doctor
```

Không chạy `sudo pip` và không cài bằng Python hệ thống. Nếu `.venv.broken` đã
tồn tại, hãy đổi sang một tên backup khác trước khi chạy `mv`; không ghi đè
virtualenv cũ.

### Nguồn sentiment hoặc macro không có dữ liệu

GX/Vietnam profile dùng FireAnt cho retail social và không gọi StockTwits/Reddit
cho mã Việt Nam. FireAnt mặc định bị khóa cho đến khi APG có văn bản cho phép;
khi bị khóa, thiếu token/key/mẫu hoặc gặp rate-limit, báo cáo đánh dấu
`partial/unavailable`, không sinh `neutral` giả. CafeF/VnExpress là
`media_tone`/editorial news riêng và cũng khóa mặc định. Sau khi có phê duyệt,
thu thập RSS trước khi chạy mã mới:

```bash
tradingagents-gx --env-file .env.postgres-hosted media collect --once --ticker FPT
tradingagents-gx --env-file .env.postgres-hosted media status
```

Với `vn_media`, mã truyền trực tiếp bằng `--ticker` không cần nằm trong
watchlist. Báo cáo đã tạo trước lần collect không tự cập nhật; dữ liệu được thu
thập sau cutoff 15:00 không được dùng ngược cho run `--date` của ngày đó. Muốn
dùng bài sau 15:00, tạo run mới bằng `--as-of-now` (hoặc shorthand
`--as-of-now --collect-evidence`) với cutoff sau thời điểm bài được thấy.

Xem cấu hình FireAnt tại
[vietnam-social-sentiment.md](vietnam-social-sentiment.md) và RSS báo Việt Nam
tại [vietnam-editorial-media.md](vietnam-editorial-media.md).

Macro Việt Nam dùng archive NSO/SBV riêng. Không có dữ liệu thì chạy:

```bash
tradingagents-gx --env-file .env.postgres-hosted macro collect --once
tradingagents-gx --env-file .env.postgres-hosted macro show \
  --as-of 2026-08-18 --json
tradingagents-gx --env-file .env.postgres-hosted doctor --live-macro
```

Xem mapping, PIT, staleness và lịch collector tại
[vietnam-macro.md](vietnam-macro.md). `FRED_API_KEY` không ảnh hưởng GX News.

### Mã không tồn tại

Kiểm tra ticker viết hoa và không có `.VN`, ví dụ dùng `FPT` thay vì `FPT.VN`.

## 9. Lưu ý an toàn

- Không commit OpenAI key, database password hoặc DSN chứa password.
- Không truyền password trực tiếp trên command line vì có thể vào shell history.
- Rotate ngay credential từng được gửi trong chat hoặc log.
- Pipeline chỉ sinh báo cáo phân tích, không gửi lệnh tới công ty chứng khoán.
- Kết quả là thông tin tham khảo, không phải bảo đảm lợi nhuận hay tư vấn đầu tư
  cá nhân hóa.
