# Audit UI/UX Dashboard Seamless Migrate

Tanggal: 15 Oktober 2026. Bahasa laporan: Indonesia. Acuan produk: PRD FR-18 dan SDD bagian 16.

## 1. Kesimpulan audit awal

Dashboard memiliki fondasi kuat untuk operator migrasi: langkah berikutnya terlihat, status memakai teks dan ikon, tindakan berisiko memakai konfirmasi, dan data gagal dimuat tidak ditampilkan sebagai kondisi aman. Perbaikan paling mendesak ada pada pemulihan halaman gagal dimuat, akses keyboard ke data yang bergulir, dan keterbacaan layar kecil.

Ditemukan **8 temuan yang dapat direproduksi: 1 P1 dan 7 P2**. Tiga peluang desain dicatat terpisah; tidak dianggap pelanggaran atau hasil riset pengguna. Tidak ditemukan P0 dalam cakupan pemeriksaan ini. Pernyataan tersebut bukan jaminan bebas cacat.

**Hasil akhir:** delapan temuan sudah diremediasi pada worktree di bawah. Hasil pengujian ulang dan bukti sesudah perubahan ada di bagian 9. Laporan membedakan kondisi baseline dari hasil remediasi; label UI-xx di sini hanya milik audit ini, bukan ID journey pada QASuite.

## 2. Baseline dan batas cakupan

| Item | Baseline |
| --- | --- |
| Repository | `imransetiadi/seamless-vms-migration` |
| Snapshot yang diaudit | `ea0d58c4`, branch `improve/jev-loop-2026-10-12b` |
| Worktree remediasi | `.worktrees/track-c-uiux-audit`, branch `fix/uiux-audit-20261015` |
| Runtime | Build Vite produksi dengan `VITE_SEAMLESS_MOCK=1`, localhost port 4188 |
| Browser utama | Playwright Chromium 156.0.8078.4 |
| Browser tambahan | WebKit 27.2 dari cache build 2359, pemeriksaan keyboard terarah; bukan Safari perangkat nyata |
| Lebar layar | 320, 390, 768, 1280 CSS px, tinggi 900 px; pengujian interaksi tambahan tinggi 800/844 px |
| Tema | Dark dan light |
| Peran | Admin untuk matriks render; viewer/operator/approver/admin lewat suite interaksi |
| Data | Fixture sintetis lokal; tidak menjalankan migrasi cloud nyata |

Snapshot ini berbeda dari `main` lokal `239009bf`: 335 commit hanya pada snapshot audit dan 3 hanya pada main saat diperiksa. Fitur Users dari PR #10 berada pada main; tidak ada pada route snapshot audit. Ketidakhadiran Users di snapshot ini **bukan temuan regresi**. File Users pada main dibaca sebagai konteks, tetapi runtime dan API fitur tersebut tidak diaudit ulang. Dua file spesifikasi Advisor yang sudah untracked tidak diubah.

Audit mencakup struktur informasi, visual hierarchy, navigasi, forms, dialog, feedback, role clarity, keyboard/focus, warna, teks, responsive, loading/error/empty, dan pemulihan kegagalan. Pengukuran performa produksi, load test cloud, uji pengguna, serta sertifikasi WCAG berada di luar cakupan.

## 3. Metode dan bukti

1. Membaca SDD bagian 16, PRD FR-18, design system, route map, komponen bersama, halaman, dan tes terkait.
2. Membangun bundle mock terpisah dari layanan pengguna. Memeriksa 15 URL x 4 lebar x 2 tema = **120 kombinasi** setelah data fixture selesai dimuat.
3. Mengukur overflow dokumen, memeriksa judul halaman, menangkap screenshot, dan menjalankan axe-core dengan tag WCAG 2 A/AA, 2.1 AA, 2.2 AA serta best practice.
4. Membuka tab Details/Audit/Input, dialog pembuatan plan, menavigasi detail dan Back, menutup form belum tersimpan, serta menguji tooltip di tepi viewport.
5. Memblokir unduhan satu chunk halaman untuk menguji pemulihan kegagalan, tanpa mengubah backend.
6. Menjalankan suite dashboard dan browser yang sudah tersedia. Memeriksa text spacing dan reduced motion secara terarah.

Hasil baseline:

| Pemeriksaan | Hasil | Batas makna |
| --- | --- | --- |
| `npm test` | 90 file, 1.759 tes lulus | Unit/component/jsdom; bukan seluruh perilaku browser |
| `npm test -- src/a11y.test.tsx src/theme/contrast.test.ts` | 130 tes lulus, bagian dari total di atas | jsdom memperingatkan canvas getContext tidak tersedia; kontras render diperiksa lagi di browser |
| `CI=1 npm run test:e2e -- --workers=2` | 37 tes Chromium lulus | Fixture mock; bukan cloud nyata |
| Build Vite mock | Berhasil | Tidak dipakai sebagai bukti performa produksi |
| Matriks 120 kombinasi | Tidak ada overflow horizontal dokumen; tidak ada pageerror normal | Tidak menjamin isi tiap panel mudah dibaca; UI-04/UI-05 tetap bermasalah |
| Axe matriks | 4 kasus `scrollable-region-focusable`, semuanya Network di migration detail pada 320/390, dua tema | Chromium otomatis memfokuskan sebagian scroller; perlu bukti browser lain |
| Axe tab tambahan | Audit: masalah scroller pada 320/390/768; Input: scroller pada semua empat lebar | Tab bukan bagian dari pemindaian default 120 kombinasi |
| Text spacing | Enam halaman, 320 px: tidak ada overflow dokumen | Judul policy makin sempit; tanpa overflow bukan berarti terbaca |
| Reduced motion | Durasi animasi/transisi drawer `0.00001s` | Pemeriksaan CSS terarah, bukan seluruh animasi produk |

Bukti mentah: [matriks baseline](audits/uiux-2026-10-15/before-results.json), [interaksi](audits/uiux-2026-10-15/before-followup.json), [tab dan form](audits/uiux-2026-10-15/before-deep.json), [spacing dan motion](audits/uiux-2026-10-15/before-extra.json).

Catatan integritas bukti: pembacaan awal Back/form terlalu cepat sempat melihat state lama. Hasil final setelah menunggu mount/close adalah filter kosong dan draft kosong, sesuai `before-followup.json`; pengukuran awal yang tidak valid tidak disertakan. Build WebKit bawaan Playwright tidak tersedia; pemeriksaan tambahan memakai executable cache yang sudah ada.

## 4. Prioritas temuan

P1 = alur utama berhenti tanpa pemulihan di dalam produk. P2 = informasi atau kontrol sulit dijangkau, kehilangan konteks/input, atau aksesibilitas terganggu pada kondisi yang dapat diulang. P3 = peluang efisiensi yang perlu validasi tugas pengguna.

| ID | Prioritas | Masalah | Lokasi baseline |
| --- | --- | --- | --- |
| UI-01 | P1 | Chunk halaman gagal membuat seluruh aplikasi kosong | `dashboard/src/App.tsx:26` |
| UI-02 | P2 | Scroller data tidak punya akses keyboard eksplisit | `NetworkPanel.tsx:118`, `RecommendationDetail.tsx:93`, `RecommendationDetail.tsx:160` |
| UI-03 | P2 | Tooltip di bawah layar terpotong | `dashboard/src/components/Term.tsx:21` |
| UI-04 | P2 | Nilai evidence menyempit menjadi 55 px | `dashboard/src/components/EvidenceCard.tsx:45` |
| UI-05 | P2 | Badge meremas judul kartu policy | `dashboard/src/pages/AdvisorPolicies.tsx:63` |
| UI-06 | P2 | Back dari detail menghapus filter daftar Plans | `dashboard/src/pages/Plans.tsx:63` |
| UI-07 | P2 | Escape menghapus input plan tanpa pemberitahuan | `dashboard/src/components/PlanCreateDialog.tsx:295` |
| UI-08 | P2 | Identitas, role, dan Demo hilang dari tampilan ringkas | `dashboard/src/components/Layout.tsx:282` |

Nomor baris menunjuk snapshot baseline; setelah remediasi gunakan nama simbol/komponen untuk mencari lokasi yang bergeser.

### UI-01 - Pemulihan kegagalan halaman

**Bukti:** buka Overview, blokir request `/assets/Plans-*.js`, lalu pilih Plans. Browser melaporkan `Failed to fetch dynamically imported module`; `#root` kosong. Screenshot: [layar kosong](audits/uiux-2026-10-15/before-chunk-failure-1280.png).

**Dampak:** gangguan jaringan atau pergantian asset saat rilis dapat menghilangkan seluruh kontrol dashboard. Operator tidak mendapat penjelasan atau tindakan pemulihan.

**Penyebab:** `Page` hanya memakai Suspense. Suspense menangani pending, bukan rejected import/render error.

**Perbaikan minimum:** boundary di sekitar halaman, mempertahankan navigasi, menjelaskan kegagalan dan menyediakan Reload page. Reset boundary saat route berubah. Reload harus tindakan eksplisit; jangan otomatis berulang atau mengirim ulang aksi migrasi.

**Kriteria selesai:** chunk ditolak menghasilkan pesan yang terlihat dan terbaca assistive technology; nav masih bekerja; kembali ke route lain berhasil; reload setelah jaringan pulih menampilkan halaman kembali.

### UI-02 - Data bergulir harus dapat dioperasikan keyboard

**Bukti:** pada 390 px, tabel NIC memiliki lebar 322 px dan isi 353 px. Axe menandai scroller tanpa fokus. Chromium terbaru tetap dapat Tab dan ArrowRight pada scroller otomatis; ini tidak boleh dilaporkan sebagai kegagalan Chromium. WebKit 27.2 dengan Option+Tab berpindah dari Edit network langsung ke Edit probes, melewati tabel. Tab biasa WebKit mengikuti pengaturan keyboard platform dan bukan dasar temuan.

Audit tab dan Input recommendation juga memakai pembungkus tanpa `tabIndex`. Input mempunyai tinggi maksimum 28 rem. Komponen `ScrollRegion` sudah tersedia dan dipakai pada destination inventory.

**Dampak:** pengguna keyboard pada browser yang tidak memfokuskan scroller otomatis tidak bisa membaca kolom/JSON yang tersembunyi.

**Perbaikan minimum:** pakai `ScrollRegion` untuk tabel tanpa kontrol, termasuk tabel verification sejenis; beri nama dan fokus pada JSON scroller. Jangan menjadikan semua div dapat ditab.

**Kriteria selesai:** Tab/Option+Tab mencapai region bernama, arrow keys menggeser isi, outline terlihat, dan axe tidak lagi menandai tiga lokasi tersebut. Tabel tetap mempertahankan caption dan header semantik.

### UI-03 - Tooltip keluar viewport

**Bukti:** pada Advisor 320x800, letakkan term confidence dekat tepi bawah lalu hover. Tooltip berada di y=781 sampai 847; 47 px melewati viewport. [Screenshot](audits/uiux-2026-10-15/before-tooltip-bottom-320.png). Posisi hanya memakai `rect.bottom + 6`. Button disabled reason memakai pola penempatan sama; itu merupakan cakupan perbaikan bersama, bukan reproduksi kegagalan kedua yang sudah terbukti.

**Dampak:** penjelasan confidence/readiness yang dibutuhkan untuk mengambil keputusan tidak terbaca penuh. Memindahkan pointer ke bagian tersembunyi tidak menyelesaikan masalah.

**Perbaikan minimum:** ukur note yang sudah dirender, tampilkan di atas jika ruang bawah kurang, batasi pada viewport, dan perbarui/hilangkan posisi saat scroll/resize. Pertahankan hoverable, Escape, focus, dan tap.

**Kriteria selesai:** batas note berada minimal 8 px dari viewport pada atas/bawah, dua tema, 320/390/1280; Escape hanya menutup note pertama kali jika berada dalam dialog.

### UI-04 - Evidence pecah menjadi potongan kata

**Bukti:** recommendation `rec-d4e6f8a09001`, 320 px. Nilai pertama hanya 55.2 px lebar, tinggi 120 px. Label efek 96 px dan link sumber memakai baris yang sama dengan nilai `flex-1`. [Screenshot](audits/uiux-2026-10-15/before-advisor_recommendations_rec-d4e6f8a09001-320-dark.png).

**Dampak:** operator harus merangkai potongan kata untuk memahami estimasi dan temuan sebelum approve/override. Lolos pemeriksaan overflow tidak menjamin keterbacaan.

**Perbaikan minimum:** pada layar kecil, pisahkan metadata efek/link dan nilai penuh; pertahankan susunan ringkas pada desktop. Tidak perlu tabel baru atau menghilangkan evidence.

**Kriteria selesai:** nilai memakai setidaknya 200 px pada viewport 320, label/link tetap jelas, urutan baca masuk akal, dan semua evidence tetap tersedia melalui Show all.

### UI-05 - Judul policy terjepit

**Bukti:** pada 320 px, judul Finance hanya 79.8 px; Built-in default 60.4 px. Keduanya menjadi empat baris, termasuk pemotongan di dalam kata. Dengan text spacing, Built-in default mendekati satu karakter per baris. [Screenshot](audits/uiux-2026-10-15/before-advisor_policies-320-dark.png).

**Dampak:** membandingkan scope dan policy lebih lambat, terutama saat memperbesar teks. Badge lebih dominan daripada nama objek yang diubah/dihapus.

**Perbaikan minimum:** beri nama dan ID lebar penuh pada mobile, badge baris sendiri; susunan berdampingan hanya ketika cukup ruang.

**Kriteria selesai:** judul setidaknya 240 px pada 320 px viewport; kata normal tidak dipotong; badge, ID, tombol edit/delete tetap terlihat pada text spacing tambahan.

### UI-06 - Konteks daftar hilang saat kembali

**Reproduksi:** Plans, ketik `Legacy`, buka Legacy apps, gunakan Back. Query menjadi string kosong. Search/status/sort disimpan hanya dengan state komponen.

**Dampak:** operator mengulangi pencarian tiap memeriksa plan lain. Tautan daftar juga tidak dapat membawa konteks filter ke rekan kerja.

**Perbaikan minimum:** simpan query, status, dan sort di URL dengan validasi nilai yang dikenal; perubahan input memakai replace agar riwayat tidak berisi setiap keystroke. Tambahkan Clear filters dengan fokus kembali ke search.

**Kriteria selesai:** filter dan urutan bertahan setelah Back/reload; URL invalid kembali ke default aman; query tidak memengaruhi otorisasi/data server.

### UI-07 - Input form terbuang saat Escape

**Reproduksi:** buka New migration plan, isi nama, tekan Escape, buka lagi. Nama kosong. Form di-reset setiap open; close langsung menutup tanpa memeriksa perubahan.

**Dampak:** konfigurasi panjang seperti mapping, pilihan VM, window, dan probe dapat hilang karena tombol keyboard yang tidak disengaja. Ini kehilangan input lokal, bukan kehilangan plan tersimpan atau data cloud.

**Perbaikan minimum:** gunakan konfirmasi discard yang sudah ada dalam produk untuk plan yang berubah. Keep editing mempertahankan nilai; Discard membuangnya. Form tak berubah tetap menutup langsung; pending request tetap tidak dapat ditutup. Jangan menyimpan kredensial atau draft ke storage browser.

**Kriteria selesai:** nama maupun pilihan VM memicu proteksi; Keep editing mempertahankan input/fokus; Discard menutup; submit berhasil tidak menanyakan discard. Edit plan memiliki perlakuan sama tanpa mengubah patch API.

### UI-08 - Konteks sesi hilang ketika navigasi ringkas

**Bukti:** session admin fixture: pada 390 dan 768 px, Demo, rina, Admin tidak tampak pada layar utama; pada 1280 px ketiganya tampak. Pada mobile tersedia setelah membuka drawer; pada tablet setelah memperluas rail. Ini mengikuti desain SDD saat ini, sehingga perbaikan memerlukan pembaruan kontrak tampilan.

**Dampak:** operator tidak langsung melihat identitas/role aktif dan apakah mode demo berjalan. Role disabled reason membantu ketika kontrol digunakan, tetapi tidak menggantikan konteks sebelum aksi.

**Perbaikan minimum:** ringkasan sesi kecil pada konten ketika sidebar penuh tidak terlihat; jangan menjejalkan nama panjang ke header 320 px. Pakai data `/me` dan `/health` yang sudah ada, jangan menebak label Production dari `demo=false`.

**Kriteria selesai:** identitas dan role tersedia tanpa membuka menu pada 320/390/768; Demo hanya muncul jika server menyatakan demo; nama panjang wrap; tidak menggeser prioritas downtime secara berlebihan.

## 5. Evaluasi per halaman

| Area | Kekuatan yang perlu dipertahankan | Masalah/peluang |
| --- | --- | --- |
| Login | Label token, paste/password manager, show token, role hint, 401/429 yang spesifik | Akses awal bergantung pada pemberian token; Users main perlu audit terpisah |
| Shell/navigation | Grouping Migrations/Clouds/Activity/Advisor, skip link, current page, drawer keyboard, theme persisten | UI-01, UI-08; rail 11 px perlu diuji dengan pengguna low vision |
| Overview | Downtime sekarang sebelum grafik historis; approval terpisah; gagal/rollback tidak dibaca sukses | Mobile 390: tinggi sekitar 4.549 px; perlu shortcut menuju panel saat antrean membesar |
| Plans | Status turunan, progres/done dan cloud tetap tersedia mobile; peran jelas | UI-06; recovery empty filter dapat dipercepat |
| Plan detail | Actions/validation/waves/approval/finalize satu konteks; konfirmasi konsekuensi | Mobile fixture sekitar 5.010 px; anchor bagian dapat membantu |
| Migration detail | Next step, downtime, lifecycle, network, probes, evidence dan timeline lengkap | UI-02, UI-03; sekitar 5.319 px pada fixture 390 |
| Providers | Source/destination terpisah; kondisi koneksi dan credentials dibedakan; retry batch menyebut kegagalan | Enam kartu sekitar 3.737 px; pencarian baru layak saat jumlah provider meningkat |
| Inventory source | Filter OS/project/readiness; VM held tetap dijelaskan; create plan memakai seleksi | Tabel mobile memprioritaskan VM/readiness; data tersembunyi perlu jalur detail yang tetap jelas |
| Inventory destination | Network/flavor/quota memiliki region keyboard dan unit IEC | Banyak tabel mendatar sah untuk data dua dimensi, perlu petunjuk scroll bila pengguna kesulitan |
| Events | Pause/resume, download shown vs whole trail terpisah; riwayat dibatasi | 200 event awal menghasilkan sekitar 26.807 px pada 390; peluang P3 untuk ukuran halaman lebih kecil |
| Advisor | Recommendation dibedakan dari keputusan; policy, confidence, asal model, data terkirim dijelaskan | UI-03; kartu layanan sebelum tugas utama menambah scroll pada mobile |
| Recommendation detail | Evidence, distribution, audit, input hash dapat diperiksa; link balik ke fakta | UI-02 dan UI-04 |
| Policies | Scope/prioritas policy jelas, built-in read-only, edit/delete menyebut objek | UI-05 |
| Advisor audit | Filter, CSV/JSON, nama aktor/target, older records | Kolom lebar memerlukan scroll lokal; ukuran layar besar tetap efisien |
| Not found | Judul semantik dan jalan kembali ke Overview | Jalur recovery cukup; tidak disamakan dengan API gagal |

Tinggi halaman adalah pengukuran fixture saat capture, bukan batas produk atau prediksi skala produksi.

## 6. Aksesibilitas, visual, dan state

- **Kontras:** token dua tema diuji; scan browser normal tidak menemukan `color-contrast`. Tidak menyimpulkan semua kombinasi hover/disabled/chart pasti patuh.
- **Warna dan ikon:** status membawa teks/ikon, angka unit IEC konsisten, chart menyediakan tabel data. Pertahankan agar pengguna tidak bergantung pada warna.
- **Typography:** Fira Sans/Fira Code dibundel; suite browser juga menguji font gagal. Teks 12 px dominan pada metadata; ukuran kecil bukan otomatis pelanggaran, tetapi perlu uji keterbacaan dengan operator.
- **Responsive:** tidak ada overflow dokumen pada matriks. Scroll tabel dua dimensi berbeda dari overflow seluruh halaman. UI-04/UI-05 menunjukkan perlunya pemeriksaan lebar kolom, bukan hanya `scrollWidth` dokumen.
- **Keyboard:** drawer, modal, fokus setelah aksi, target 24 px/spasi dan tombol utama 44 px telah diuji suite. UI-02 adalah celah scroller; UI-03 celah posisi penjelasan.
- **Form dan dialog:** nama/label/hint/summary error jelas; footer tidak menutup fokus; validasi serta maker-checker tetap perlu dipertahankan. UI-07 menutup celah pembatalan input lokal.
- **Loading:** skeleton dan status bernama tersedia. Pemindaian dilakukan setelah loading usai; screenshot saat masih memuat tidak dipakai sebagai bukti layout final.
- **Error/retry:** tes memeriksa failed load tidak berubah menjadi empty/all-clear, retry fokus, kredensial parsial, dan export gagal. UI-01 berbeda: kegagalan kode halaman belum tertangani.
- **Empty:** daftar/filter kosong punya penjelasan. Plans belum punya clear langsung pada baseline; masuk UI-06.
- **Live/feedback:** event pause/resume, reconnect dan outcome diumumkan. Bukti ini dari mock dan tes, belum mengukur gangguan SSE produksi berjam-jam.
- **Copy:** banyak penjelasan teknis sesuai persona operator. Referensi `SDD` di deskripsi panel lebih cocok sebagai bantuan lanjutan; jangan mengganti panduan operasional dengan nomor spec.

## 7. Rencana remediasi dan peluang lanjutan

Urutan pengerjaan: UI-01, UI-02/UI-03, UI-04/UI-05, kemudian UI-06/UI-07/UI-08. Gunakan komponen bersama dan API yang ada; tidak perlu dependency baru, desain ulang total, atau backend baru.

| Kelompok | Ruang perubahan | Bukti penerimaan |
| --- | --- | --- |
| Recovery | Boundary route | Fault injection chunk; nav dan reload |
| Keyboard/tooltip | ScrollRegion, Term, disabled note | Axe render, Tab/arrow, viewport bounds |
| Mobile readability | EvidenceCard, PolicyCard | Lebar isi 320/390, text spacing, dua tema |
| Flow continuity | Plans URL, plan discard | Back/reload, nilai invalid, keep/discard, role |
| Session clarity | Shell | Identity/role/demo semua lebar tanpa duplikasi desktop |

Peluang P3, perlu keputusan desain setelah melihat tugas operator:

1. Kurangi jumlah event yang dirender per batch atau tambahkan navigasi ke filter ketika jejak sangat panjang. Pertahankan ekspor keseluruhan dan urutan audit; jangan langsung menambah virtualisasi.
2. Tambahkan tautan bagian untuk halaman detail panjang jika observasi pengguna menunjukkan banyak scroll bolak-balik. Jangan sembunyikan downtime/next step di tab.
3. Pindahkan referensi internal SDD ke help detail sambil menjaga instruksi perbaikan yang dibutuhkan operator. Uji copy dengan pengguna baru sebelum merombak terminologi.

Peluang tersebut bukan cacat terverifikasi atau kewajiban redesign. Ukuran batch, susunan informasi, dan istilah tidak diubah hanya untuk membuat screenshot lebih pendek.

## 8. Batas pengujian dan risiko tersisa

- Tidak menghubungkan OpenStack/VMware nyata, menguji token produksi, atau menjalankan cutover/finalize nyata.
- Tidak melakukan studi tugas dengan pengguna, screen reader VoiceOver/NVDA, perangkat iOS/Android nyata, atau matriks penuh Firefox/Safari. WebKit hanya pemeriksaan keyboard terarah dengan binary cache.
- Lebar 320 adalah uji CSS reflow; bukan bukti seluruh perilaku zoom 400% browser atau text zoom 200%.
- Tidak mengaudit ulang backend/auth atau runtime Users pada main. Tidak mencampur hasil feature Users sebelumnya dengan hasil audit snapshot ini.
- Hasil tes otomatis tidak setara sertifikasi WCAG. Cakupan diperluas lewat interaksi dan inspeksi visual, tetapi data produksi panjang dan keadaan jaringan nyata tetap perlu diuji sebelum rilis.

## 9. Hasil remediasi

Status: **UI-01 sampai UI-08 selesai dan diverifikasi lokal**, tanpa dependency baru atau perubahan API migrasi. Patch berada pada `fix/uiux-audit-20261015`, berdasarkan snapshot audit `ea0d58c4`. Snapshot ini sengaja tidak diganti dengan main yang berbeda jauh.

| Temuan | Perubahan aktual | Bukti penerimaan |
| --- | --- | --- |
| UI-01 | `PageErrorBoundary` mempertahankan shell, menawarkan Reload page, reset saat pathname berubah | Fault injection, navigasi keluar, kembali, dan reload lulus; [panel recovery](audits/uiux-2026-10-15/after-chunk-recovery-1280.png) |
| UI-02 | `ScrollRegion` pada NIC, audit recommendation, dan verification probes; JSON region bernama dan fokus | Axe matriks dan tab bersih; Option+Tab WebKit kini fokus `NICs of app-billing-01`, `tabIndex=0` |
| UI-03 | Term dan disabled note mengukur ruang render, berpindah ke atas jika perlu, dibatasi viewport | Tooltip 320x800 berubah dari y=781..847 menjadi y=683..749; tes disabled explanation juga lulus; [tooltip](audits/uiux-2026-10-15/after-tooltip-bottom-320.png) |
| UI-04 | Grid mobile memberi nilai evidence satu baris lebar penuh; metadata/link tetap terlihat | Lebar nilai 55.2 menjadi 228 px, tinggi 120 menjadi 40 px; [evidence](audits/uiux-2026-10-15/after-advisor_recommendations_rec-d4e6f8a09001-320-dark.png) |
| UI-05 | Nama/ID policy memakai satu baris layout penuh sebelum badge pada mobile | Kedua judul 254 px, tinggi 24 px; spacing tambahan tanpa overflow; [policy](audits/uiux-2026-10-15/after-advisor_policies-320-dark.png), [spacing](audits/uiux-2026-10-15/after-policy-spacing-320.png) |
| UI-06 | Query/status/sort/direction memakai URL dan nilai enum divalidasi; Clear filters mengembalikan fokus | Back/reload mempertahankan filter; status/sort dan parameter tidak dikenal diuji |
| UI-07 | `ConfirmDialog` menggantikan form sementara sebelum discard; input tetap di state komponen | Keep editing mempertahankan nama/pilihan VM dan fokus; Discard mengembalikan fokus; form kosong menutup langsung; save existing tetap lulus; [konfirmasi](audits/uiux-2026-10-15/after-discard-plan-320.png) |
| UI-08 | Ringkasan nama/role/Demo di konten saat sidebar penuh tidak terlihat | Satu konteks terlihat pada 320/390/768/1280; test jam downtime pada layar pertama 390x844 tetap lulus |

Pengujian akhir:

| Command/pemeriksaan | Hasil |
| --- | --- |
| `npm test` | **90 file, 1.759 tes lulus**; [ringkasan output](audits/uiux-2026-10-15/unit-result.txt) |
| `CI=1 npm run test:e2e -- --workers=2 --retries=0` | **47 lulus**, termasuk 10 regresi audit baru; [output](audits/uiux-2026-10-15/e2e-result.txt) |
| `npm run typecheck` | Lulus, termasuk spesifikasi Playwright; [output](audits/uiux-2026-10-15/typecheck-result.txt) |
| `npm run lint` | Lulus dengan `--max-warnings 0`; [output](audits/uiux-2026-10-15/lint-result.txt) |
| `npm run build` | Build produksi dan bundle gate lulus: 5 startup chunks, 281.9 KiB, tanpa lazy-only chunk; [output](audits/uiux-2026-10-15/build-result.txt) |
| Matriks 120 kombinasi setelah patch | **0 axe violations, 0 pageerror, 0 overflow dokumen**; [JSON](audits/uiux-2026-10-15/after-results.json) |
| 16 kasus tambahan tab Audit/Input | Kedua tema x empat lebar x dua tab: **0 axe violations**; [JSON dan ukuran aktual](audits/uiux-2026-10-15/after-verify.json) |
| 12 kasus tepi tooltip | Dua tema x lebar 320/390/1280 x tepi atas/bawah: seluruh tooltip berada dalam viewport; [JSON](audits/uiux-2026-10-15/tooltip-matrix.json) |
| WebKit 27.2 terarah | Option+Tab mencapai region NIC bernama; bukan klaim keseluruhan Safari lulus |

Tujuh skenario regresi awal gagal pada baseline sebelum patch. Tiga pemeriksaan tambahan menguatkan validasi URL, perubahan pilihan VM saja, dan disabled note. Satu eksekusi suite penuh sempat timeout 30 detik pada tes Plans saat mesin menjalankan beberapa pemeriksaan; suite penuh diulang dan lulus tanpa melonggarkan timeout. Warning canvas dari jsdom tetap ada; tidak dianggap bukti kontras browser.

Jev advisory memverifikasi tiga klaim pemeriksaan; klaim jumlah tes browser masih ditandai untuk review dengan confidence rendah. Output Playwright lengkap tetap menjadi bukti langsung, diperiksa ulang jumlah dan statusnya; verdict advisory tidak digunakan sebagai sertifikasi produk.

Revalidasi sebelum PR pada basis terbaru `origin/main` juga lulus: `npm test` **82 file, 1.578 tes**, `CI=1 npm run test:e2e -- --workers=2 --retries=0` **43 lulus**, typecheck, lint, production build, `npm audit --omit=dev --audit-level=high`, dan dua pemindaian gitleaks. Angka ini mencerminkan basis PR terbaru; angka 1.759/47 di tabel sebelumnya tetap bukti snapshot audit `ea0d58c4`.

Batas remediasi: proteksi draft berlaku pada Cancel/Escape form plan. Menutup tab browser atau berpindah lewat browser Back masih dapat membuang input lokal; tidak ada autosave. Tiga peluang P3 pada bagian 7 tetap rekomendasi untuk riset tugas, bukan fitur baru yang ditambahkan tanpa bukti. Belum ada deployment atau perubahan pada layanan cloud nyata.

Untuk mengulang regresi terarah dari worktree ini, jalankan `cd dashboard && npm run test:e2e -- e2e/audit.spec.ts`. Suite membuat build mock sendiri pada port 4173; hentikan preview lama pada port itu atau gunakan `CI=1` agar tidak memakai bundle kedaluwarsa.
