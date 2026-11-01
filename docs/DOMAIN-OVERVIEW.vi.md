# Domain Overview — NguyenHuuThinh Workflow 1.1.0

> Tài liệu dành cho SA/Senior Dev chưa biết domain. Phân tích theo: **Problem → Requirement vs Solution → Objectives → Tech**.
> Nguồn: README, docs/, NOTICE, config/preset.json, `hongguo_lite/`, `tools/season*.py`, `vendor/core/source_adapter/README.md`, `validation/REPORT.md`.
> Chú thích: **[Đọc]** = có bằng chứng trong repo; **[Suy ra]** = tôi suy luận, cần xác nhận với tác giả; **[Chưa kiểm]** = chưa chạy.

> Nhãn bổ sung: **[Web]** = thông tin từ tìm kiếm web, link ở mục 8. Thông tin web có thể đã đổi; chưa tự kiểm từng nguồn gốc.

---

## 0. Triết lý thiết kế (Design Philosophy)

### 0.1 Một câu
> **Không tự xây công nghệ nhận diện/dịch/tổng hợp tiếng. Ghép các công cụ có sẵn tốt nhất thành một dây chuyền, rồi bao quanh bằng cơ chế chứng cứ để biết chính xác cái gì đã được kiểm, cái gì chưa.**

Đây là kiến trúc kiểu **"integrator + auditor"**: giá trị của dự án không nằm ở thuật toán AI (đều đi mượn), mà ở **(a) cách ghép và (b) cách chứng minh kết quả**.

### 0.2 Họ giải pháp đã chọn, và các họ giải pháp không chọn [Suy ra từ code, đối chiếu Web]

| Hướng | Ví dụ | Dự án chọn? | Lý do có vẻ như vậy |
|---|---|---|---|
| **A. Công cụ "tất cả trong một"** | pyvideotrans, VideoCaptioner [Web] | Không dùng làm lõi. Chỉ **mượn ý tưởng/code** | Hộp đen; tự quyết định cắt/chậm video; khó chứng minh từng bước |
| **B. Làm tay hoàn toàn trong CapCut** | | Không | Không lặp lại được, mất nhiều tuần cho cả bộ phim |
| **C. Dịch vụ lồng tiếng AI thương mại** | | Không | Mất kiểm soát tên riêng, tiếng động nền, chất lượng; không có bằng chứng |
| **D. Dây chuyền ghép công cụ + cổng người duyệt** | | **Có** | Giữ quyền kiểm soát từng khâu, thay thế được từng bộ phận |

### 0.3 Dự án "lợi dụng" cái gì (Leverage)

| Việc cần làm | Lợi dụng cái có sẵn | Phần dự án tự thêm | Rủi ro khi phụ thuộc |
|---|---|---|---|
| Lấy video chuẩn | **Giao thức App Hongguo** qua adapter Node (fork `drpys`, GPL-3.0). Chỉ nhận đúng 1080p HEVC 30fps | Kiểm FFprobe + giải mã đầy đủ, khoá thư mục đầu ra, ghi định danh + SHA256 | Không phải API chính thức; có thể bị chặn bất cứ lúc nào [Đọc] |
| Lấy lời Trung + mốc thời gian | **CapCut** Auto captions (dịch vụ ASR của CapCut) | Đọc draft bằng `pyCapCut`, kiểm khớp timeline | Cần thao tác tay trên UI; `pyCapCut` còn ở v0.0.3 và tác giả đang chuyển công sức sang `pyJianYingDraft` [Web] |
| Dịch có ngữ cảnh | **Con người / LLM (Codex)** | Mẫu `editorial-draft.json`, ép KEEP/EXCLUDE + bằng chứng hash | Chất lượng phụ thuộc người; mã không đánh giá được nghĩa |
| Kiểm tên riêng | **PaddleOCR** (PP-OCRv5) | Chỉ OCR câu có tên, tại khung giữa câu; 3 loại quyết định (`name_policy.py`) | OCR không đủ để tự duyệt, nên bắt người xem ảnh |
| Giọng Anh | **edge-tts** — thư viện Python gọi dịch vụ TTS của Microsoft Edge, không phải API chính thức [Web] | Khoá tốc độ gốc `+0%`, từ chối nếu dịch vụ từ chối, không đổi giọng | Có thể bị Microsoft đổi/chặn bất kỳ lúc nào [Web]. Tên giọng `AvaMultilingualNeural` tôi **chưa xác nhận** được trên web; cần `edge-tts --list-voices` |
| Co thời gian cho vừa khung | Ý tưởng **autorate** của `pyvideotrans` (docstring `edge_fit.py` ghi rõ) + FFmpeg `atempo` | Đo theo mẫu PCM, **không cắt phần có tiếng, không làm chậm video**, retry khi vượt | Xem 0.5 — ngưỡng 5× rất cao so với khuyến nghị gốc |
| Tách hiệu ứng khỏi thoại/nhạc | **MVSEP-CDX23** (Demucs v4/HT huấn luyện trên DnR) [Web] | Chạy GPU FP32, chỉ chọn stem `sfx`, giữ bằng chứng mọi stem | Xem 0.5 — mô hình huấn luyện trên dữ liệu không phải phim ngắn Trung Quốc |
| Ghép video | **FFmpeg** (x264 CRF21) | Hộp mờ + chữ vàng + logo theo preset, đo PTS | |
| Chạy lại, truy vết | **Content-addressed cache** (như Make/Bazel) tự làm trong `store.py` | Signature đầu vào, receipt đầu ra, hash từng file | Cứng với sửa đổi (mục 5) |

### 0.4 Bảy nguyên tắc thiết kế (rút từ code và tài liệu)

1. **Mượn công nghệ, tự làm bằng chứng.** Không viết ASR/TTS/tách nguồn; viết hợp đồng và kiểm tra.
2. **Chứng cứ thay cho lời khẳng định.** Mọi "đã duyệt" phải có file + SHA256 thật (`season_contracts.validate_review`). Điểm tin cậy OCR không được coi là duyệt.
3. **Chỉ-ghi-thêm, bất biến.** Run mới cho phim mới; sau `seal` không sửa. Lỗi giữa chừng giữ nguyên attempt thay vì xoá.
4. **Fail closed, không fallback im lặng.** Không CPU, không đổi giọng, không đổi nguồn, không đổi model; sai hash là dừng.
5. **Thu hẹp bài toán bằng profile cứng.** Chỉ 1080p HEVC CFR30 SDR; mọi thứ khác bị loại để khỏi phải xử lý vô số trường hợp.
6. **Máy làm việc xác định, người làm việc phán đoán.** Dịch, duyệt tên, xem hình, nghe — luôn là cổng người.
7. **Trung thực về giới hạn.** Kết quả mặc định là `DRAFT`; nói rõ "chưa nghe", "chưa thử trên máy thứ hai", "SFX stem chưa chứng minh sạch".

### 0.5 Đánh đổi chính (Trade-offs) — chỗ triết lý va vào thực tế

| Quyết định | Được | Mất | Ghi chú đối chiếu [Web] |
|---|---|---|---|
| **Không bao giờ làm chậm video, không cắt thoại**; chỉ tăng tốc giọng 1.25–5× | Timeline gốc, lip-sync gần đúng, SFX vẫn khớp hình | Giọng có thể quá nhanh, khó nghe | pyvideotrans khuyến nghị tăng tốc âm tối đa khoảng **1.8×** *kèm* làm chậm video, và nói bảo đảm vừa khít mọi trường hợp là "gần như bất khả thi" (docs của họ). Dự án này cho phép tới **5×** và luôn ≥1.25×. Đây là rủi ro chất lượng cần nghe thử thật |
| Chỉ giữ stem SFX −5 dB | Tiếng động nền sống động, không còn thoại Trung | Mất nhạc nền; SFX có thể lẫn thoại/nhạc | Mô hình CDX23 dựa Demucs v4, huấn luyện trên DnR (LibriSpeech + FMA + FSD50k); tác giả thừa nhận tập thi khác xa dữ liệu huấn luyện. Phù hợp lời cảnh báo trong README |
| Ghép nhiều công cụ ngoài | Mỗi bộ phận thay được | Nhiều điểm lỗi ngoài tầm kiểm soát (CapCut UI, Ava, App Hongguo) | |
| Bất biến + hash mọi thứ | Truy vết tuyệt đối | Sửa lỗi nặng nề, dễ vỡ khi sửa code (xem mục 5) | |

### 0.6 Mẫu kiến trúc có thể gọi tên
- **Pipeline / Pipes-and-Filters** với cổng phê duyệt giữa chừng (human-in-the-loop gate).
- **Content-addressed build cache** (`signature` + `receipt`).
- **Append-only ledger / audit trail** cho thay đổi biên tập (`canonical-revision.json`).
- **Anti-corruption layer / Adapter** quanh mỗi thành phần ngoài (source adapter Node, worker từng venv).
- **Process isolation theo role** (5 venv) để tránh xung đột phụ thuộc ML.
- **Fail-closed validation** ở mọi ranh giới.

---

## 1. Problem (Vấn đề nghiệp vụ)

### 1.1 Bối cảnh
- **Hongguo (红果短剧)** [Web]: ứng dụng phim ngắn của **ByteDance**, ra mắt năm 2023, **xem miễn phí, thu tiền bằng quảng cáo** và chia sẻ doanh thu cho nhà sáng tạo; hưởng lợi từ lưu lượng Douyin. Nhiều bài báo ngành nêu quy mô hàng trăm triệu người dùng (số liệu mỗi nguồn một khác, chưa đối chiếu); Tencent cũng ra dịch vụ cạnh tranh trong WeChat. Mỗi bộ gồm nhiều tập (README ví dụ `1-N`, tài liệu source adapter ví dụ `1-80`), video 1080p có **phụ đề tiếng Trung đốt cứng (hard-sub)** trên hình [Đọc].
- Hệ quả cho dự án: nội dung gốc được phát miễn phí **trong App**, nhưng quyền phân phối/dịch lại thuộc chủ nội dung (xem 1.3).
- Nhu cầu: biến **một bộ phim Trung hoàn chỉnh** thành **bản tiếng Anh**: lồng tiếng Anh, thay phụ đề Trung bằng phụ đề Anh, giữ nguyên hình và tiếng động nền.

### 1.2 Vì sao khó (Pain points)
| # | Khó khăn | Hệ quả nếu bỏ qua |
|---|---|---|
| P1 | Chữ Trung nằm **trong pixel** của video, không có file sub rời | Phải tự tìm lại thời điểm và nội dung từng câu thoại |
| P2 | Bản dịch máy từng câu không có ngữ cảnh | Sai nghĩa, sai tên nhân vật giữa các tập |
| P3 | **Tên riêng** (nhân vật, địa danh) phải nhất quán cả bộ | Một nhân vật bị gọi bằng nhiều tên |
| P4 | Tiếng Anh thường **dài hơn** tiếng Trung cho cùng ý | Giọng đọc không vừa khung thời gian của câu thoại |
| P5 | Âm thanh gốc trộn lẫn thoại + nhạc + hiệu ứng | Không thể thay thoại mà giữ hiệu ứng (tiếng cửa, va chạm...) |
| P6 | Sửa tay từng bước dễ lệch đồng bộ, khó lặp lại khi làm hàng chục tập | Kết quả mỗi lần một kiểu, không truy được lỗi từ đâu |
| P7 | Bộ phim dài (nhiều tập, GPU nặng) | Mất công nếu phải làm lại từ đầu sau lỗi giữa chừng |

### 1.3 Rủi ro nghiệp vụ/pháp lý [Đọc]
- Pipeline tải nguồn phim qua một **adapter dịch ngược giao thức App** (fork `drpys`, GPL-3.0) [Đọc: `vendor/core/source_adapter`]. Phụ thuộc dịch vụ bên ngoài, có thể bị chặn (401/403/429).
- `NOTICE.md` nói rõ: giấy phép phần mềm **không** cấp quyền đối với phim hay branding. `package` không đăng tải công khai.
- Đây là điểm cần quyết định chính sách của chủ dự án (xem mục 6), không phải việc kỹ thuật.

---

## 2. Requirement vs Solution

Phần này tách **cái cần đạt** (Requirement) khỏi **cách đang làm** (Solution) để bạn đánh giá độc lập từng cái.

| ID | Requirement (cần gì) | Solution hiện tại (làm thế nào) | Đánh giá |
|---|---|---|---|
| R1 | Có text Trung + timing từng câu | **CapCut tự nhận diện** (Auto captions → Chinese), đọc lại bằng thư viện `pyCapCut` (`tools/season_capcut.py`) | Thủ công, nhưng chính xác. Là điểm phụ thuộc UI bên ngoài |
| R2 | Bản dịch có ngữ cảnh, nhất quán | **Người/Codex dịch** vào file `editorial-draft.json`, mỗi câu phải KEEP/EXCLUDE kèm bằng chứng hash. Có glossary riêng cho phim | Chất lượng phụ thuộc người dịch. Code chỉ kiểm độ phủ |
| R3 | Tên riêng đúng | Chọn các câu có tên → **OCR** (PaddleOCR PP-OCRv5) đọc chữ Trung trên khung hình giữa câu → so với glossary → quyết định `MATCH_KEEP` / `UNREADABLE_KEEP` / `MISMATCH_USE_OCR` (`hongguo_lite/name_policy.py`) | Không dùng điểm tin cậy OCR để tự duyệt, vẫn cần người |
| R4 | Phụ đề Trung cũ biến mất, phụ đề Anh thay vào | Đo **hộp làm mờ (blur_box)** thủ công cho từng tập, làm mờ vùng đó và vẽ chữ Anh vàng lên (`preset.json`: Arial Bold, 44px@1080p, tối đa 2 dòng) | Thủ công, nhưng hộp phải đo mỗi phim vì vị trí khác nhau |
| R5 | Thoại Anh vừa khung thời gian câu | Giọng **Ava** (`en-US-AvaMultilingualNeural`, backend `edge-tts`, tốc độ gốc +0%), sau đó **co thời gian** (tempo 1.25–5×) bằng FFmpeg `atempo`, **đo bằng mẫu PCM**, tuyệt đối **không cắt phần có tiếng và không làm chậm video** (`edge_fit.py`) | Nếu vượt 5× thì `NEEDS_EDIT`, phải sửa bản dịch cho ngắn lại |
| R6 | Giữ hiệu ứng, bỏ thoại + nhạc gốc | **MVSEP-CDX23** (tách nguồn Demucs, GPU FP32): chỉ lấy stem `sfx`, trộn **−5 dB**; thoại Anh 0 dB; có limiter | Tách nguồn không hoàn hảo: README nói loại bỏ stem khác **không chứng minh** SFX hoàn toàn sạch thoại/nhạc |
| R7 | Giữ nguyên chất lượng và thời gian gốc | Chỉ nhận **1920×1080 HEVC, CFR 30fps, SDR yuv420p**; frame, sample, thứ tự tập được giữ; xuất x264 CRF21 | Hẹp: profile khác bị từ chối |
| R8 | Lặp lại được, truy được, không ghi đè | Mỗi bước là **stage có chữ ký (signature) đầu vào + receipt + hash đầu ra** (`store.py`); mỗi run là thư mục mới, bất biến sau khi `seal` | Mạnh về truy vết, tốn việc khi muốn sửa (xem mục 5) |
| R9 | Có thể cài trên máy khác | `setup` tải runtime ghim hash, tách 5 role venv, ngoài thư mục source | Online, chưa kiểm trên máy thứ hai [Đọc: REPORT.md] |
| R10 | Kết quả trung thực về chất lượng | Mọi đầu ra gắn nhãn **DRAFT** đến khi có người nghe và duyệt ngữ nghĩa | Chủ đích |

---

## 3. Objectives (Mục tiêu hệ thống)

Từ R1–R10 rút ra mục tiêu cho kiến trúc, theo thứ tự ưu tiên mà code đang thể hiện [Suy ra]:

1. **Trung thực & truy vết** (không bao giờ khẳng định đã duyệt khi chưa duyệt) — quan trọng nhất; có mặt ở mọi nơi.
2. **Tái lập được** (khóa phiên bản, hash, run bất biến).
3. **Không làm hỏng timeline gốc** (không cắt thoại, không đổi tốc độ video, giữ frame/sample).
4. **Fail-fast, không fallback âm thầm** (không CPU fallback, không đổi giọng/nguồn/model).
5. **Giảm công thủ công ở các bước máy làm tốt** (tải, tách tiếng, ghép) nhưng giữ người ở các bước phán đoán (dịch, duyệt tên, nghe).

Ngoài phạm vi (non-goals) [Đọc]: offline portable, CPU, nhiều profile video, đăng tải, tự duyệt chất lượng dịch.

---

## 4. Tech (Kiến trúc & công nghệ)

### 4.1 Luồng nghiệp vụ (ở mức nhìn một lượt)

```
 [Nguồn]            [Con người]                  [Máy]                         [Đầu ra]
 init/preflight ─►  CapCut: Auto captions ─►     extract (đọc draft)
 download (Node)    (nhận diện Trung)            │
 prepare (tạo draft CapCut mới)                  ▼
                                      editorial-export ─► người dịch (KEEP/EXCLUDE, tên)
                                                          editorial-import
                                      geometry-prepare ─► người đo blur_box từng tập
                                                          geometry-import
                                      ocr ─► người duyệt tên ─► names-import
                                      speech  (Ava TTS + co thời gian)
                                      background (MVSEP: lấy SFX)
                                      video (ghép: mờ + chữ Anh + logo + audio)
                                      review-prepare ─► người xem contact sheet ─► review-record
                                      pts ─► finish ─► package ─► seal ──────────► 1 MP4 + 1 SRT
                                                                                   + lineage + SHA256
```
Vai trò: ô trái/giữa là **tự động**, ô "Con người" là **cổng chặn** (hệ thống dừng tới khi có bằng chứng hợp lệ).

### 4.2 Phân tầng (layering)

| Tầng | Thành phần | Trách nhiệm |
|---|---|---|
| Launcher | `workflow.ps1`, `setup.ps1` | Chọn Python controller, truyền lệnh. Không logic nghiệp vụ |
| Orchestrator | `tools/season.py` + `tools/season_*.py` | Điều phối từng lệnh, khóa runtime, ngân sách ổ đĩa, kiểm run đã seal |
| Domain/lib | `hongguo_lite/` | Hợp đồng (`season_contracts`), cache `Store`, fit âm thanh, chính sách tên, midpoint hình, media |
| Vendor/engine | `vendor/core/` | Worker chạy trong venv riêng: TTS, OCR/tách nguồn; adapter Node tải nguồn |
| Runtime | Ngoài repo (`%LOCALAPPDATA%\...\runtime-1.1.0`) | 5 venv (controller/edge/paddle/separation/capcut), Node, FFmpeg, model |
| Dữ liệu run | `runs/<tên>/` | State, receipt, evidence, delivery (bất biến sau seal) |

Đặc điểm kiến trúc đáng chú ý: **mỗi role một venv** để tránh xung đột phiên bản (Paddle, Torch, edge-tts, pyCapCut cần bản khác nhau); controller gọi các role qua process con.

### 4.3 Mô hình dữ liệu cốt lõi
- **Run**: một lần sản xuất một phim; thư mục mới, không dùng lại.
- **Stage**: đơn vị công việc có `key`, `inputs → signature`, `receipt` (đầu ra + hash). Cache theo signature; sai hash là không tái dùng (`store.py`).
- **Cue**: một câu thoại, có `cue_id`, khung hình bắt đầu/kết thúc, `canonical_text`, `proper_names`.
- **Evidence**: file + SHA256, buộc mọi quyết định của người vào một bằng chứng cụ thể.
- **Generation**: mỗi lần sinh video có pointer `active-*-generation.json` + `dependency-snapshot.json`; sửa bản dịch tạo generation mới, không xoá cái cũ.

### 4.4 Công nghệ

| Mối quan tâm | Công nghệ | Ghi chú |
|---|---|---|
| Ngôn ngữ chính | Python 3.12 (controller + worker), Node ≥22 (adapter tải) | |
| Media | FFmpeg/FFprobe 9.0.2, MediaInfo | x264 CRF21, AAC 192k, 48kHz |
| Nhận diện lời Trung | CapCut Auto captions (thủ công) + `pyCapCut` | Chỉ đọc draft |
| OCR | PaddleOCR PP-OCRv5 mobile (GPU, CUDA 11.8) | Chỉ cho câu có tên |
| TTS | `edge-tts` (Ava) — **dịch vụ online của Microsoft** | Cần internet; không có giọng thay thế |
| Tách nguồn | MVSEP-CDX23 / Demucs (GPU FP32) | Dùng khoá GPU toàn máy |
| Khoá phiên bản | `--require-hashes`, `config/locks/*`, `downloads.lock.json` | |
| Test | `unittest/pytest` ~78 test Python + 6 test Node [Đọc: REPORT.md] | Không có CI [Đọc] |
| Giấy phép | Pha trộn GPL-3.0 (adapter, VideoCaptioner), MIT, Apache-2.0 | Xem `NOTICE.md` |

---

## 5. Đánh giá thiết kế dưới góc nhìn SA

**Điểm hợp lý với domain**
- Chọn *stage + receipt + hash* là đúng cho pipeline dài, tốn GPU, và có người xen vào giữa.
- Phân biệt rõ "máy làm" và "người phán" — tránh sai lầm tự động hoá phần cần phán đoán.
- Tách venv theo role giải quyết đúng vấn đề xung đột ML.

**Rủi ro kiến trúc**
| Rủi ro | Giải thích |
|---|---|
| Thiếu phản hồi khi **thay đổi code** | Có phản hồi về *dữ liệu* (hash, doctor), không có phản hồi về *thay đổi* (CI, test hợp đồng). Sửa một module có thể phá run cũ mà không ai biết |
| Bất biến cản trở sửa lỗi | Muốn sửa phải tạo run mới hoặc theo quy trình `revision` rất nặng (`canonical-revision.json`) |
| Hai tầng code trùng vai | `hongguo_lite/` và `vendor/core/src/hongguo_ssot/` (`fresh_media.py`, `fresh_media_v3.py`) |
| Phụ thuộc dịch vụ ngoài không kiểm soát | Ava (Microsoft), nguồn phim (giao thức App), CapCut UI |
| Quy mô test nhỏ | ~78 test cho hơn 15 stage, nhiều stage cần GPU/mạng |
| Profile video hẹp | Mọi thứ khác bị từ chối ngay |

---

## 6. Câu hỏi mở cần hỏi tác giả (trước khi quyết định đầu tư)

1. Mục đích sử dụng cuối: nội bộ/thử nghiệm hay phát hành? (quyết định mức độ rủi ro pháp lý)
2. Số phim/tháng dự kiến? (nếu ít thì quy trình thủ công chấp nhận được; nếu nhiều thì cần tự động hoá khâu người)
3. Điểm nghẽn thực tế nhất hiện nay: dịch, đo blur_box, hay chờ GPU?
4. Có chấp nhận thêm một lớp CI/test để bảo vệ khi sửa code không?
5. Có định mở rộng sang profile video khác, ngôn ngữ khác, giọng khác không?

## 7. Thuật ngữ

| Từ | Nghĩa trong dự án |
|---|---|
| Hongguo (红果短剧) | App phim ngắn miễn phí của ByteDance, thu tiền bằng quảng cáo [Web] |
| Cue | Một câu thoại có thời gian |
| Hard-sub | Phụ đề đốt cứng trong hình |
| blur_box | Hình chữ nhật che phụ đề cũ để vẽ phụ đề Anh |
| Ava | Giọng TTS `en-US-AvaMultilingualNeural` |
| SFX stem | Luồng hiệu ứng âm thanh tách từ audio gốc |
| Tempo fit | Co thời gian giọng đọc cho vừa khung câu (1.25–5×) |
| CFR | Constant Frame Rate — tốc độ khung hình cố định |
| PTS | Presentation timestamp — mốc thời gian từng frame |
| Run / Seal | Một lần sản xuất / niêm phong, không sửa nữa |
| DRAFT | Trạng thái mặc định đến khi người nghe và duyệt thật |

## 8. Nguồn tham khảo (Web)

Các nguồn dưới đây đến từ tìm kiếm web ngày 2026-11-01. Tôi chưa mở đọc đầy đủ từng trang, chỉ dùng phần tóm tắt trong kết quả tìm kiếm.

| Chủ đề | Nguồn |
|---|---|
| Hongguo: miễn phí, quảng cáo, ByteDance, quy mô | [Caixin Global](https://www.caixinglobal.com/2025-08-22/in-depth-micro-drama-platforms-knock-down-paywalls-as-viewers-lose-interest-102354440.html) · [eWeek](https://www.eweek.com/news/bytedance-hongguo-ai-short-drama-china-streaming-apac/) · [FoxData](https://foxdata.com/en/blogs/bytedances-next-global-hit-after-tiktok-inside-the-meteoric-rise-of-hongguo-short-drama-app/) · [SCMP](https://www.scmp.com/tech/tech-trends/article/3367664/bytedances-ai-enhanced-short-drama-app-eclipses-chinas-netflix-rivals-combined) |
| Đối thủ: Tencent trong WeChat | [TechNode](https://technode.com/2026/10/14/tencent-launches-free-short-drama-service-inside-wechat/) · [Yicai Global](https://www.yicaiglobal.com/news/wechat-launches-mini-program-to-rival-bytedances-short-drama-app) |
| Căn thời gian giọng dịch (autorate, giới hạn 1.8×) | [pyvideotrans — align](https://pyvideotrans.com/en/align) · [pyvideotrans — vì sao lệch](https://pyvideotrans.com/en/09) |
| Tách nguồn điện ảnh (dialog/effect/music) | [MVSEP-CDX23 (GitHub)](https://github.com/zfturbo/mvsep-cdx23-cinematic-sound-demixing) · [Sound Demixing Challenge 2023 — Cinematic Track (arXiv)](https://arxiv.org/pdf/2308.06981) |
| pyCapCut | [GitHub](https://github.com/GuanYixuan/pyCapCut) · [PyPI](https://pypi.org/project/pycapcut/) |
| edge-tts (rany2) | [Edge TTS guide](https://videosdk.live/developer-hub/ai/edge-tts) · [PyPI plugin tham chiếu](https://pypi.org/project/ovos-tts-plugin-edge-tts/) |
| VideoCaptioner | [WEIFENG2333/VideoCaptioner mirror](https://github.com/gebederry/VideoCaptioner) · [tác giả](https://github.com/weifeng2333) |

Điểm cần tự xác nhận: tên giọng `en-US-AvaMultilingualNeural` có đang tồn tại trong dịch vụ không; `pyCapCut` còn được bảo trì không; Hongguo có điều khoản về dịch/lồng tiếng lại nội dung không.
