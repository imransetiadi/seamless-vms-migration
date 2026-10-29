# Audit UI/UX Dashboard Seamless Migrate

Tanggal: 29 Oktober 2026. Status: audit selesai; temuan belum diremediasi.
Basis: `main`, HEAD `9421748d6b44f3f0c939836b1ea5d2b55cc6d1ca`, termasuk perubahan lokal yang sudah ada sebelum audit.

Dashboard melayani operator migrasi VM: menghubungkan cloud, memilih VM, menyusun plan, memantau downtime, memberi approval, memverifikasi hasil, dan menelusuri keputusan advisor. Audit mengasumsikan penggunaan desktop operasional dengan akses mobile untuk pemantauan/approval, empat role, nama resource panjang, dan riwayat yang terus bertambah.

**Verdict: fondasi Chromium baik, tetapi belum memenuhi pengalaman lintas browser dan kasus data nyata secara konsisten.** Ada **12 temuan: 2 P1, 7 P2, 3 P3**. P1 diprioritaskan sebelum menyatakan dukungan antarmuka lintas browser siap. Tidak ditemukan P0 dalam lingkup yang diperiksa. Angka ini bukan skor kepatuhan WCAG atau bukti kesiapan produksi.

## 1. Temuan berprioritas

P1 = mengganggu tugas utama atau keterbacaan halaman. P2 = menghilangkan konteks/input, memberi informasi keliru, atau menghambat akses. P3 = friksi penggunaan atau ketidakkonsistenan pedoman. Semua temuan berasal dari worktree saat ini; hasil audit lama tidak dihitung sebagai bukti baru.

### UI-01 - P1: nama resource panjang merusak reflow halaman

- **Konteks:** Overview menampilkan nama VM yang sedang down; Providers menampilkan nama cloud.
- **Masalah/bukti:** Fixture API dengan nama tanpa spasi sepanjang 128 karakter menghasilkan overflow halaman Overview **942 px** dan Providers **935 px** pada viewport 320 px. Pada 768 px: 582/575 px; pada 1440 px: 642/63 px. Ini overflow halaman, bukan scroll tabel yang memang diperlukan. Nama provider tidak dibatasi panjangnya pada model `Provider`.
- **Lokasi:** `dashboard/src/pages/Overview.tsx:116`, `dashboard/src/pages/Overview.tsx:200`, `dashboard/src/pages/Providers.tsx:242`; kontrak `seamless/src/seamless_migrate/domain/models.py:132`.
- **Reproduksi:** Kembalikan VM gagal dengan `vm.name = 'v'.repeat(128)` dan provider dengan `name = 'p'.repeat(128)`, buka `/` dan `/providers` pada 320 px. Tidak perlu memodifikasi DOM atau CSS untuk memicu masalah.
- **Perbaikan terkecil:** Terapkan pembungkusan intrinsic-width-safe (`min-w-0` dan `overflow-wrap:anywhere`) pada nama/flex item terkait. Periksa nama yang sama pada attention list, breadcrumb, kartu, dan heading. Jangan memotong identitas dengan ellipsis atau menutupi masalah memakai `overflow-x:hidden` global.
- **Jika dibiarkan:** Identitas dan tindakan keluar dari viewport; operator harus menggeser seluruh dashboard untuk membaca insiden.
- **Penerimaan:** Nama VM/provider/plan 128-200 karakter tanpa spasi tetap terbaca pada 320/390/768/1440 px; `documentElement.scrollWidth <= innerWidth`; tabel lebar boleh punya scroller lokal bernama.

### UI-02 - P1: klik target pilihan VM tidak andal di WebKit

- **Konteks:** Checkbox VM merupakan langkah awal membuat plan; label 44 x 44 px menjadi target sentuhnya.
- **Masalah/bukti:** Tiga tes `VM selection stays reviewable, touchable and keyboard-safe` gagal di WebKit 27.2 pada 320/768/1440 px: klik label tidak menghasilkan status checked yang diharapkan. Ketiganya kembali gagal dengan satu worker. Probe terpisah mengonfirmasi klik label pada 320 px tetap unchecked, sedangkan fokus checkbox + Space berhasil. Probe sederhana pada 768/1440 berhasil: kegagalan bergantung urutan/state, bukan semua klik WebKit gagal.
- **Lokasi:** `dashboard/src/components/VmTable.tsx:321`, `dashboard/src/components/VmTable.tsx:376`; regresi saat ini `dashboard/e2e/audit.spec.ts:275`.
- **Reproduksi:** Jalankan tes tersebut di WebKit dengan build mock saat ini; ikuti pilihan dua VM, filter, Selected only, clear, lalu ulangi pada tema kedua.
- **Perbaikan:** Telusuri urutan pointer/focus/change dan pergeseran scroll saat fokus pada wrapper tabel. Pertahankan asosiasi label native dan target 44 px; jangan menambah toggle manual yang bisa menggandakan aktivasi. Akar penyebab belum dipastikan oleh audit.
- **Jika dibiarkan:** Operator mengira VM sudah dipilih, tetapi jumlah pilihan dan isi plan tidak berubah.
- **Penerimaan:** Satu klik/tap pada pusat dan tepi label memilih tepat satu kali; Space tetap bekerja; seluruh alur pilihan lulus Chromium dan WebKit pada kedua tema tanpa retry tes.

### UI-03 - P2: keyboard dan pemulihan fokus belum konsisten di WebKit

- **Konteks:** Tabel horizontal, dialog panjang, dan drawer harus tetap dapat dipakai keyboard.
- **Masalah/bukti:** Region NIC berukuran 322 px dengan konten 393 px tetap `scrollLeft=0` setelah ArrowRight di WebKit; Chromium bergerak ke 40 px. Probe dialog provider menemukan `document.activeElement === BODY` pada dua titik dalam 30 Tab, sementara Chromium tidak. Pembukaan dialog melalui keyboard memulihkan fokus dengan benar pada kedua engine; pembukaan melalui pointer di WebKit tidak kembali ke tombol pembuka. Suite WebKit juga menemukan fokus tertutup dalam dialog provider/plan.
- **Lokasi:** `dashboard/src/components/ScrollRegion.tsx:12`, `dashboard/src/components/Modal.tsx:85`, `dashboard/src/components/Modal.tsx:152`, `dashboard/src/components/ProviderDialog.tsx:541`.
- **Reproduksi:** Pada 390 px fokuskan region `NICs of app-billing-01`, tekan ArrowRight. Pada 1280 x 800 buka Add provider, fokus Name, lalu Tab 30 kali. Bandingkan pembukaan/penutupan New plan dengan pointer dan keyboard.
- **Perbaikan:** Sesuaikan pengelolaan fokus dengan urutan tab browser; berikan ref pemicu eksplisit untuk pemulihan pointer bila dibutuhkan. Pastikan scroller dapat dioperasikan pada engine yang didukung. Periksa fokus setelah scroll stabil, bukan hanya satu frame selama animasi.
- **Jika dibiarkan:** Pengguna kehilangan posisi fokus atau tidak dapat membaca kolom yang tersembunyi.
- **Penerimaan:** Fokus tidak jatuh ke body/browser selama dialog aktif, kontrol fokus terlihat, Escape kembali ke pemicu, dan keyboard dapat menjangkau seluruh konten tabel. Uji pengaturan navigasi keyboard Safari pada perangkat nyata sebelum mengklaim dukungan Safari penuh.

### UI-04 - P2: perlindungan draft hanya mencakup sebagian jalan keluar

- **Konteks:** Provider/policy berisi banyak konfigurasi; plan sudah punya dialog discard.
- **Masalah/bukti:** Isi Name pada Add provider atau New policy, tekan Escape, buka kembali: nama kosong dan tidak ada konfirmasi. Pada New plan, isi nama lalu browser Back: dialog hilang tanpa discard, meskipun Escape/Cancel sudah dilindungi.
- **Lokasi:** `dashboard/src/components/ProviderDialog.tsx:540`, `dashboard/src/components/PolicyDialog.tsx:139`, `dashboard/src/components/PolicyDialog.tsx:185`, `dashboard/src/components/PlanCreateDialog.tsx:587`.
- **Perbaikan terkecil:** Gunakan pola dirty/discard plan untuk provider dan policy, lalu lindungi navigasi keluar/reload saat draft berubah. Simpan draft hanya di memori; kredensial tidak perlu masuk browser storage.
- **Jika dibiarkan:** Pekerjaan konfigurasi hilang akibat satu tombol Escape atau Back; pengguna mengisi ulang konfigurasi dan rahasia.
- **Penerimaan:** Escape, Cancel, Back, dan reload pada draft berubah memberi pilihan yang sesuai. Form bersih dan save sukses tidak meminta discard. Keep editing mempertahankan nilai serta fokus.

### UI-05 - P2: token sekali tampil bisa hilang sebelum disimpan

- **Konteks:** Admin membuat user; token hanya tersedia pada respons pertama.
- **Masalah/bukti:** Dalam fixture autentikasi aktif, buat `audit-user`, pindah ke Overview tanpa Copy/Done, lalu kembali ke Users. User tetap ada, token tidak tersedia, dan tidak ada konfirmasi meninggalkan token. Peringatan teks sudah ada. Penghapusan token saat navigasi memang sesuai SDD; celahnya adalah pencegahan kehilangan akses sebelum pengguna mengaku sudah menyimpannya.
- **Lokasi:** `dashboard/src/pages/Users.tsx:63`, `dashboard/src/pages/Users.tsx:75`, `dashboard/src/api/hooks.ts:133`.
- **Perbaikan:** Minta pengakuan bahwa token sudah disimpan sebelum meninggalkan hasil, atau konfirmasi kehilangan token pada navigasi. Jelaskan langkah pemulihan yang benar-benar tersedia melalui admin/CLI. Jangan mempersist token untuk menutupi masalah ini.
- **Jika dibiarkan:** User terdaftar tetapi penerimanya belum memiliki kredensial; dashboard tidak menyediakan pemulihan token tersebut.
- **Penerimaan:** Copy gagal tetap mendukung salin manual; navigasi sebelum pengakuan memberi peringatan; Done/navigasi terkonfirmasi menghapus token dari memori dan cache sebagaimana sekarang.

### UI-06 - P2: komponen Field menampilkan wajib tetapi tidak meneruskan validasi native

- **Konteks:** Users mengandalkan form HTML untuk nama wajib dan pola nama.
- **Masalah/bukti:** `TextField` mengambil prop `required`, tetapi hanya memasang `aria-required`. Pada field nama user kosong, browser melaporkan `required=false`, `aria-required=true`, `checkValidity()=true`, dan `form.checkValidity()=true`. Add user mengirim permintaan kosong, lalu menampilkan error umum `Choose a valid user name and role.[validation_error]`; tidak ada error inline nama.
- **Lokasi:** `dashboard/src/components/Field.tsx:99`, `dashboard/src/components/Field.tsx:108`, `dashboard/src/pages/Users.tsx:46`, `dashboard/src/pages/Users.tsx:88`. Pola yang sama ada pada SelectField dan TextAreaField.
- **Perbaikan terkecil:** Teruskan atribut native `required` pada kontrol bersama; form `noValidate` tetap memakai validasi khususnya. Kaitkan kesalahan nama pada input, termasuk penolakan server seperti nama duplikat.
- **Jika dibiarkan:** Field yang tampak wajib lolos submit dan memerlukan round trip sebelum pengguna tahu letak kesalahan.
- **Penerimaan:** Submit nama kosong tidak memanggil API, fokus berada pada nama, alasan jelas; validasi custom plan/provider/policy tidak berubah. Penolakan nama duplikat mempertahankan input.

### UI-07 - P2: audit rekomendasi menganggap jendela riwayat sebagai seluruh riwayat

- **Konteks:** Tab Audit menjelaskan jejak keputusan sebuah rekomendasi.
- **Masalah/bukti:** Tab hanya memuat 1000 event audit terbaru dalam scope migration/plan lalu memfilter `target_id`. Fixture mempertahankan dua event rekomendasi target dan menambah 1001 event baru dalam migration yang sama. UI menampilkan `No audit events for this recommendation yet.` tanpa cara memuat event lama. Request teramati: `?tail=true&limit=1000&migration_id=mig-d4e6f8a090&audit=true`.
- **Lokasi:** `dashboard/src/pages/RecommendationDetail.tsx:85`, `dashboard/src/api/hooks.ts:356`.
- **Perbaikan terkecil:** Bedakan hasil kosong pada jendela yang dimuat dengan riwayat yang benar-benar kosong; sediakan Load older memakai parameter `before` yang sudah tersedia. Berikan batas/jendela yang jelas bila hasil belum lengkap.
- **Jika dibiarkan:** Operator menyimpulkan keputusan tidak memiliki audit meskipun catatannya masih tersimpan.
- **Penerimaan:** Fixture dengan 1001+ event lebih baru tetap dapat menemukan dua event lama; hasil kosong tidak mengklaim seluruh sejarah sudah diperiksa. Tidak perlu mengubah kontrak API untuk pagination tersebut.

### UI-08 - P2: Back menghilangkan konteks investigasi dan pilihan Pause

- **Konteks:** Operator menyaring daftar, membuka detail, lalu kembali untuk memeriksa item berikutnya.
- **Masalah/bukti:** Search migrations `analytics-node-1`, buka detail sampai heading tampil, lalu Back: pencarian kosong. Search VMs `web-01`, pindah Plans, Back: kosong. Events dengan search `web-01` dan Pause, pindah Plans, Back: query kosong dan live updates aktif lagi. Plans sendiri sudah mempertahankan filter melalui URL.
- **Lokasi:** `dashboard/src/components/MigrationsTable.tsx:100`, `dashboard/src/components/VmTable.tsx:88`, `dashboard/src/pages/Events.tsx:170`, `dashboard/src/pages/AdvisorAudit.tsx:46`.
- **Perbaikan:** Gunakan pola URL Plans untuk filter/sort daftar yang perlu dipulihkan; simpan state Pause per kunjungan/session tanpa mencampurnya dengan status koneksi. Tabel pemilihan di dialog tidak harus memakai URL global.
- **Jika dibiarkan:** Operator mengulang filter dan kehilangan posisi investigasi; daftar Events kembali bergerak tanpa pilihan Resume.
- **Penerimaan:** Detail-Back mengembalikan query/filter/sort/halaman; Pause tetap berlaku sampai Resume. Clear filters tetap mengembalikan fokus ke pencarian.

### UI-09 - P2: Events menyebut Live ketika stream terputus

- **Konteks:** Halaman Events menampilkan status koneksi dan ringkasan pembaruan.
- **Masalah/bukti:** Fixture SSE menutup stream setelah heartbeat. Halaman memperlihatkan `Event stream: Reconnecting` bersamaan dengan `Live · waiting for new events`. `statusText` ditentukan dari Pause/jumlah event, bukan status transport.
- **Lokasi:** `dashboard/src/pages/Events.tsx:340`; status koneksi tersedia pada `dashboard/src/api/LiveEventsProvider.tsx`.
- **Perbaikan terkecil:** Turunkan teks ringkasan dari status stream yang sudah dipakai LiveIndicator. Gunakan Connecting/Reconnecting/Stopped dan jelaskan data terakhir yang masih ditampilkan; pertahankan Pause sebagai preferensi pengguna.
- **Jika dibiarkan:** Operator mengira data masih mengalir saat koneksi sedang terputus.
- **Penerimaan:** Stream ditutup, offline, unauthorized, dan pulih menghasilkan pesan konsisten; tidak ada klaim Live selama transport bukan open; pembaruan status tidak membanjiri pembaca layar.

### UI-10 - P3: penanganan entitas hilang memberi Retry yang tidak menyelesaikan masalah

- **Konteks:** Tautan lama bisa menunjuk recommendation/provider yang sudah tidak ada.
- **Masalah/bukti:** `/advisor/recommendations/not-found` menampilkan error umum dengan Retry. `/inventory/not-found` menampilkan dua pesan sekaligus: provider tidak ada dan inventory tidak dapat dibaca dengan Retry. Plan/migration 404 sudah menyediakan pesan entitas hilang dan Back to plans.
- **Lokasi:** `dashboard/src/pages/RecommendationDetail.tsx:201`, `dashboard/src/pages/Inventory.tsx:264`, `dashboard/src/pages/Inventory.tsx:268`.
- **Perbaikan terkecil:** Bedakan 404 dari kegagalan sementara, berikan Back to recommendations/pemilih provider, hindari dua pesan untuk penyebab yang sama. Pertahankan Retry untuk gangguan sementara.
- **Jika dibiarkan:** Pengguna mengulang request pada objek yang sudah hilang dan menerima pesan berulang.
- **Penerimaan:** Keempat jenis detail memiliki satu pesan jelas serta jalan kembali; 503 tetap dapat dipulihkan melalui Retry.

### UI-11 - P3: pencarian insiden Advisor terlalu jauh di mobile

- **Konteks:** Advisor menggabungkan rekomendasi, pencarian insiden, catatan, dan penjelasan data yang dikirim.
- **Masalah/bukti:** Setelah fixture selesai dimuat pada 390 x 844, heading Similar incidents berada di **y=2991 px**, Recent advisor notes di 3440 px, Data sent to advisor di 4685 px. Pengguna harus melewati seluruh feed rekomendasi sebelum mencari insiden; tidak ada navigasi bagian seperti detail plan/migration.
- **Lokasi:** `dashboard/src/pages/Advisor.tsx:217`, `dashboard/src/pages/Advisor.tsx:259`; pola tersedia `dashboard/src/components/SectionNav.tsx:1`.
- **Perbaikan terkecil:** Tambahkan jalan pintas bernama ke bagian utama atau pisahkan tugas memakai tab yang aksesibel. Pertahankan rekomendasi sebagai konten utama jika itu prioritas produk; perubahan prioritas tugas perlu validasi operator.
- **Jika dibiarkan:** Pencarian yang berguna saat insiden sulit ditemukan pada layar kecil.
- **Penerimaan:** Pencarian insiden dapat dicapai dari bagian awal dengan satu tindakan; fokus mendarat pada heading/input yang terlihat. Ukur waktu mencari kasus dan keberhasilan tugas dengan operator, bukan hanya tinggi halaman.

### UI-12 - P3: master design system masih mengandung pedoman yang bertentangan

- **Konteks:** MASTER.md menjadi rujukan perubahan antarmuka berikutnya.
- **Masalah/bukti:** Kategori masih `Smart Home/IoT Dashboard`; contoh modal memakai putih literal; pola halaman menyebut Start trial/Contact; contoh motion meminta GSAP yang tidak ada pada dependency dashboard. Ini berbeda dari dashboard migrasi operasional, token dua tema, dan motion CSS aktual.
- **Lokasi:** `dashboard/design-system/seamless-migrate/MASTER.md:11`, `dashboard/design-system/seamless-migrate/MASTER.md:158`, `dashboard/design-system/seamless-migrate/MASTER.md:185`, `dashboard/design-system/seamless-migrate/MASTER.md:194`.
- **Perbaikan terkecil:** Hapus contoh landing/GSAP dan ganti dengan token serta komponen aktual. SDD bagian 16 menjadi acuan perilaku operasional. Tidak perlu dependency baru.
- **Jika dibiarkan:** Iterasi berikutnya bisa mengembalikan permukaan putih, animasi berlebih, atau CTA yang tidak sesuai tugas operator.
- **Penerimaan:** Contoh tombol/input/modal dan navigasi cocok dengan komponen berjalan; pedoman mobile, reduced motion, dan target kontrol tidak saling bertentangan.

## 2. Cakupan halaman dan alur

| Area | Tugas dan pemeriksaan | Hasil utama |
| --- | --- | --- |
| Login | Token kosong/salah, pending, tampil/sembunyi, fokus, tema, health, role | Regresi pending/rejection lulus; 4 kombinasi login tanpa pelanggaran axe |
| Shell | Sidebar, rail, drawer, skip link, identitas, tema tersimpan, chunk failure | Chromium lulus; pemulihan fokus dan chunk reload WebKit perlu perhatian |
| Overview | Downtime/SLO, approval, attention, angka unavailable, chart dan tabel alternatif | Hierarki insiden jelas; nama VM panjang memecahkan reflow |
| Plans | Search/status/sort URL, create, empty, error/retry | Persistensi filter dan draft Escape/Cancel lulus; Back draft belum dilindungi |
| Plan detail | Settings, waves, migrations, findings, edit/validate/start/finalize, section jump | Journey fixture lulus Chromium; filter daftar hilang setelah detail-Back |
| Migration detail | Lifecycle, actions, downtime, network, probes, evidence, estimates, timeline | Konfirmasi/role/anchor Chromium lulus; region keyboard WebKit bermasalah |
| Providers | Source/destination, health, check all, add/edit/delete, partial save | Error/retry dan form terstruktur; draft dan nama panjang bermasalah |
| Inventory source | Filter/sort/select/Selected only, clear, pembuatan plan dari pilihan | Target 44 px tersedia; klik pilihan WebKit belum andal |
| Inventory destination | Networks, flavors, volume types, projects, quotas | Tabel lokal bernama, tanpa overflow halaman pada fixture normal |
| Events | Filter, pause/resume, load older, download, stream reconnect | Konteks Back hilang; status Live bertentangan saat reconnect |
| Advisor | Status layanan, recommendations, search, notes, data sent | Penjelasan advisory/evidence jelas; akses search mobile panjang |
| Recommendation detail | Recommendation vs decision, evidence, Details/Audit/Input, approval | Batas 1000 event menghasilkan empty state salah; 404 perlu recovery tepat |
| Policies | Scope, threshold, mode, role, create/edit/delete | Form dan konfirmasi delete tersedia; draft Escape tidak dilindungi |
| Advisor audit | Search/action/actor, reset, older events, CSV/JSON, link target | Reset/fokus dan retry lulus; dibaca sebagai riwayat terbatas yang dapat diperluas |
| Users | Empat role, auth off/on, create, clipboard fallback, token sekali tampil | Gating role lulus; required native dan proteksi token perlu diperbaiki |
| Not found | Route tak dikenal dan entitas detail tak ditemukan | Route umum punya recovery; detail recommendation/inventory tidak konsisten |

Alur yang diuji mencakup connect provider, inventory ke plan, edit/validate plan, approval advisor, finalize, alert source VM, unduh audit, filter/Back, dialog discard, dan retry. Aksi mutasi dijalankan pada fixture lokal; tidak ada perubahan resource cloud nyata.

## 3. Evaluasi visual, aksesibilitas, dan performa

- **Hierarki:** Overview mendahulukan downtime dan tindakan manusia. Plan/migration memakai section navigation. Provider memisahkan source/destination. Nama, status, dan konsekuensi tindakan umumnya terlihat jelas. Advisor mobile membutuhkan wayfinding UI-11.
- **Konsistensi:** Fira Sans/Fira Code, spacing, kartu, ikon Lucide, dan token dua tema konsisten pada build saat ini. Light theme memakai permukaan lembut; tidak ada kebutuhan redesign menyeluruh berdasarkan bukti audit.
- **Responsif:** Fixture normal lolos 320/375/390/768/1024/1280/1440 px. Tabel yang memang dua dimensi memakai scroll lokal. Nama panjang tetap menimbulkan kegagalan UI-01; lulus fixture pendek tidak menjamin reflow data produksi.
- **Keyboard:** Skip link, fokus navigasi, dialog bertumpuk, error summary, tooltip, tab, dan aksi unavailable dicakup suite. Chromium lulus; WebKit menunjukkan celah UI-02/UI-03. Target primary 44 px dan aturan minimum/spasi 24 px diperiksa suite; bukan klaim semua link berukuran 44 px.
- **Kontras/semantik:** 56 axe scan matriks utama, 4 login, dan 3 dialog = **63 scan tanpa pelanggaran terdeteksi** pada kondisi yang dicatat. Token kontras diuji suite unit. Hasil axe bukan sertifikasi WCAG; fokus WebKit dan reflow nama panjang tetap masalah aksesibilitas.
- **Teks/motion:** 14 route lolos stress text-spacing (letter .12em, word .16em, line-height 1.5, paragraph margin 2em) pada 320 px. 14 route lolos root text-size 200% pada 1280 px dan 14 route lolos landscape 844 x 390 tanpa overflow halaman. Root text-size adalah simulasi resize teks, bukan pengujian zoom Safari/perangkat fisik. Reduced-motion menonaktifkan motion nonesensial melalui CSS; suite juga memeriksa font fallback.
- **Feedback:** Pending dan error ditampilkan, input umumnya dipertahankan, aksi besar memakai konfirmasi. Form baru tanpa `required` native, draft keluar, token sekali tampil, serta status Live masih perlu tindak lanjut.
- **Performa:** Build produksi dan bundle gate lulus: **5 startup chunks, 290.7 KiB** sesuai definisi script bundle. Chart dimuat secara lazy, font disajikan lokal; tes memastikan tidak meminta host font luar. Audit tidak mengukur Core Web Vitals lapangan, latensi cloud, atau skala ribuan VM secara end-to-end.
- **Bahasa/copy:** UI saat ini berbahasa Inggris, sesuai implementasi/acuan yang diperiksa. Tidak ditemukan kontrak ID/EN dashboard pada scope ini; audit tidak mengklaim parity terjemahan. Copy status audit/Live/404 perlu diperbaiki sebagaimana temuan.

## 4. Bukti pengujian baru

| Pemeriksaan | Hasil 29 Oktober |
| --- | --- |
| `npm test` dari dashboard | 104 file, **2303 tes lulus** |
| `CI=1 npm run test:e2e -- --workers=2 --output=/tmp/seamless-audit-20261029-e2e` | **60/60 Chromium lulus**, build mock baru |
| `npm run typecheck` | Lulus aplikasi dan E2E |
| `npm run lint` | Lulus, tanpa warning |
| `npm run build` | Lulus TypeScript, Vite, bundle gate |
| Google Chrome 156.0.8078.73 | **196 kombinasi** = 14 route x 7 lebar x 2 tema; tidak ada page error, overflow halaman, atau ellipsis terpotong pada fixture normal |
| Axe matriks/login/dialog | **63 scan tanpa pelanggaran terdeteksi** |
| Text spacing / resize teks / landscape | **14 + 14 + 14** pemeriksaan route tanpa overflow halaman pada fixture normal |
| Fault injection API 503 | **12 halaman** menampilkan error; Retry pulih pada seluruhnya |
| Empty state | Plans, Providers, Inventory source, Events diperiksa; Events menampilkan copy filter meski trail kosong |
| Missing entities | Plan, migration, recommendation, provider diperiksa |
| Role Users | Viewer/operator/approver ditolak dengan penjelasan; admin dapat mengakses form |
| Pending data | Request Plans ditahan, loading teramati, lalu hilang setelah respons diteruskan |
| WebKit 27.2, suite penuh, retry 0 | **49 lulus, 11 gagal** |
| WebKit ulang terarah, satu worker | **7/7 kasus terpilih kembali gagal**; bukan kegagalan karena paralel semata |

### Interpretasi kegagalan WebKit

Sebelas kegagalan suite terdiri dari: 3 pemilihan VM, 4 pemulihan fokus setelah pointer/dialog/drawer, 1 scroll keyboard NIC, 2 fokus tertutup dialog, 1 recovery chunk setelah Reload page. Probe terpisah membedakan jalur keyboard yang memulihkan fokus dengan benar dari jalur pointer yang tidak.

Kasus chunk recovery sudah gagal lagi pada run terarah setelah route abort dilepas. Akar masalah cache/import engine versus penanganan aplikasi belum dipastikan; jangan menyebut seluruhnya bug React atau seluruhnya masalah harness. Simpan sebagai risiko kompatibilitas yang harus ditutup sebelum klaim dukungan penuh. Coverage resmi repo hanya mengonfigurasi Chromium (`dashboard/playwright.config.ts:25`); WebKit pada audit memakai konfigurasi sementara tanpa mengubah repo.

Scan awal dialog saat animasi masuk dan login tepat saat perpindahan tema melaporkan kontras sementara. Pengukuran ulang dilakukan dengan reduced motion dan, untuk login, setelah transisi warna stabil selama 300 ms; hasil stabil tidak melaporkan pelanggaran. Hasil sementara tersebut tidak dihitung sebagai cacat kontras permanen atau sebagai scan lulus.

### Lokasi bukti lokal

Semua path berikut adalah artefak audit lokal di `/tmp`, bukan bukti produksi dan bukan storage permanen:

- `/tmp/seamless-audit-20261029-unit.log`
- `/tmp/seamless-audit-20261029-e2e.log` dan `/tmp/seamless-audit-20261029-e2e/`
- `/tmp/seamless-audit-matrix-20261029.mjs`, `/tmp/seamless-uiux-audit-matrix-20261029/results.json`, serta 56 screenshot di direktori yang sama
- `/tmp/seamless-audit-probes-20261029.mjs` dan `/tmp/seamless-audit-probes-20261029/results.json`: required, draft, token, form axe
- `/tmp/seamless-audit-states-20261029.mjs` dan `/tmp/seamless-audit-states-20261029/results.json`: error/retry, empty, audit window, nama panjang
- `/tmp/seamless-audit-extra-states-20261029.mjs` dan `/tmp/seamless-audit-extra-states-20261029/results.json`: detail error/404, role, loading
- `/tmp/seamless-audit-navigation-20261029.mjs` dan `/tmp/seamless-audit-navigation-20261029/results.json`: Back/filter/pause dan posisi section Advisor
- `/tmp/seamless-audit-text-20261029.json`, `/tmp/seamless-audit-zoom-20261029.json`, `/tmp/seamless-audit-login-20261029.json`
- `/tmp/seamless-audit-webkit.config.mjs`, `/tmp/seamless-audit-webkit-20261029.log`, `/tmp/seamless-audit-webkit-recheck-20261029.log`; trace tersimpan pada direktori output masing-masing
- `/tmp/seamless-audit-webkit-probe-20261029.mjs` dan `/tmp/seamless-audit-webkit-probe-20261029/results.json`: perbandingan engine untuk klik, scroll, fokus

Fault injection memakai build produksi pada port 4182 dengan respons dari `MockServer` repo melalui Playwright routing. Pemeriksaan utama memakai build mock pada port 4181. Probe Users mengaktifkan autentikasi hanya pada fixture browser. Tidak ada source aplikasi/backend yang diedit untuk membuat temuan lulus/gagal.

## 5. Urutan remediasi dan gate penerimaan

1. **Tutup hambatan operasional:** UI-01/UI-02, lalu UI-03 dan investigasi chunk WebKit. Tambahkan kasus nama panjang serta project browser yang memang didukung ke gate.
2. **Lindungi pekerjaan dan konteks:** UI-04/UI-05/UI-06/UI-08. Uji keluar lewat seluruh jalur, input invalid, token sekali tampil, detail-Back, dan Pause.
3. **Benarkan informasi operasional:** UI-07/UI-09/UI-10. Uji riwayat terpotong, koneksi putus/pulih, dan 404 versus 503.
4. **Rapikan discovery/pedoman:** UI-11/UI-12. Validasi tugas mobile dengan operator; sinkronkan dokumen dengan komponen yang sudah ada.

Setelah remediasi, jalankan suite unit, lint/typecheck/build, suite browser penuh, matriks dua tema, dan kasus khusus yang gagal dalam audit. Jangan memakai kelulusan Chromium saja untuk menutup temuan WebKit. Status setiap temuan baru boleh berubah setelah reproduksinya lulus tanpa melemahkan assertion.

## 6. Batas audit dan pemeriksaan penyelesaian

| Kebutuhan audit | Bukti pemenuhan |
| --- | --- |
| Seluruh scope dashboard | Route App.tsx, tabel cakupan, source halaman/komponen, matriks browser termasuk source/destination inventory dan 404 |
| Alur, state, dan error recovery | 60 journey Chromium, 12 fault/retry, empty/404/role/loading, probe input/draft/token/Back |
| Visual, responsif, aksesibilitas | Screenshot dua tema, 7 lebar, 63 axe, spacing/resize/landscape, keyboard dan cross-engine |
| Temuan yang dapat ditindaklanjuti | UI-01 sampai UI-12 memiliki bukti, lokasi, dampak, saran, dan penerimaan |
| Menjaga worktree pengguna | Diff source sebelum/sesudah identik; audit menambahkan laporan ini saja |

Tidak diperiksa langsung: produksi, control plane hidup, migrasi/provider nyata, sesi pembaca layar VoiceOver/NVDA, perangkat iOS/Android fisik, Firefox, usability study dengan operator, CVD/persepsi warna pada perangkat nyata, dan metrik performa lapangan. Semua itu tetap batas bukti, bukan dianggap lulus. Backend/API/security internal tidak diaudit selain kontrak yang diperlukan menjelaskan perilaku UI.

Kesesuaian produk yang masih perlu keputusan: browser yang dijanjikan resmi, apakah guard kehilangan token diwajibkan, dan prioritas pencarian insiden terhadap feed Advisor. Rekomendasi UI-05 menambah pencegahan kehilangan tanpa mengubah aturan penghapusan token pada SDD.

Audit selesai berarti cakupan dan laporan sudah diperiksa. Temuan terbuka tetap memerlukan remediasi; tidak ada klaim dashboard bebas masalah atau siap produksi.
