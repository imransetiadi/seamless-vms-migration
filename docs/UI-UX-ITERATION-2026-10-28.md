# Iterasi UI/UX dashboard — 28 Oktober 2026

Basis integrasi: `origin/main` pada `bd0ff9d1`, branch `improve/dashboard-uiux-20261028`.
Acuan: SDD §16 dan design system dashboard. Lingkup: frontend, tes, dan dokumentasi.
Perubahan lokal sebelumnya dibawa ke worktree terpisah; checkout asal dan dokumen di luar lingkup dipertahankan.
Fitur GPU, Hooks, timeline network/DNS dan React 19.3.1 dari main terbaru tetap dipertahankan.

| Loop | Masalah dan perubahan | Bukti saat ini |
| --- | --- | --- |
| Pilihan VM | Pilihan di luar filter/halaman sulit ditinjau. Tambah `Selected only` dan `Clear selection`; clear filter tidak menghapus pilihan. Area label checkbox tabel dibuat 44 × 44 px. Fokus kembali ke pencarian saat pilihan/baris hilang. Berlaku di Inventory dan form plan. | Tes komponen mencakup filter, pagination, clear seluruh pilihan, dan fokus setelah deselect individu/bulk. Playwright mengukur label checkbox 44 × 44 px pada 320/768/1440 px di kedua tema; alur pilihan, deselect dan fokus lulus. |
| Pergantian provider | Filter provider lama terbawa ketika inventory tujuan sudah ada di cache. Remount tabel berdasarkan source/provider, termasuk form plan. | Dua tes regresi gagal sebelum fix: pencarian masih `web-01`; lulus setelah fix. Fokus tetap di pemilih provider/source. |
| Navigasi fragment | `#%` atau encoding UTF-8 terpotong membuat `decodeURIComponent` melempar error pada shell/hook halaman. Pakai resolver bersama yang mengabaikan fragment rusak. | Dua kasus gagal sebelum fix. Tes mencakup fragment rusak, target hilang, Unicode valid, serta shell tanpa hook landing. |
| Fokus dialog | Shift+Tab dari ringkasan error sebelum kontrol pertama keluar dari dialog. Dialog bertumpuk juga tidak membuat panel bawah inert dan bisa membuka background terlalu awal saat satu dialog ditutup. Perbaiki `Modal` bersama: batas Tab mengikuti posisi DOM, hanya panel teratas aktif, background tetap terkunci sampai dialog terakhir tutup. | Regresi gagal sebelum fix; 80 tes terkait lulus setelah fix. Tes mencakup kedua urutan penutupan, pemulihan fokus, ringkasan di tengah/akhir form, dialog tanpa kontrol, serta StrictMode dan root yang sebelumnya inert. Input tersembunyi dan kontrol yang dinonaktifkan lewat fieldset dikeluarkan dari urutan fokus. Regresi reverse Tab browser lulus pada 320/1440 px. |
| Fokus tabel mobile | Tab dapat membiarkan badge/link hanya tampak sebagian di dalam tabel horizontal form plan. Gunakan `scrollIntoView({ block: 'nearest', inline: 'nearest' })` saat kontrol tabel menerima fokus. | Tes browser semula gagal untuk Encrypted volume, Legacy apps dan Image-backed root pada 375 × 700 px; lulus setelah fix. Pemeriksaan keyboard lokal mencakup 60 perpindahan Tab tanpa fokus tertutup. |
| Tema terang | Putih murni pada card dan latar hampir putih terlalu terang bagi pengguna. Ganti dengan latar `#E8EDF3`, card `#F1F4F8`, muted `#DCE3ED`, dan border/grid `#C5CFDC`. Berlaku melalui token bersama pada semua route. | 102 tes token dan kontras lulus. Pratinjau Chrome desktop dan mobile 390 × 844 px sudah diperiksa; Matriks integrasi terbaru: 196 kombinasi route/lebar/tema dan 56 pemindaian axe lulus pada palet baru. |
| Pemulihan filter aktivitas | Events dan Advisor audit belum menyediakan reset seluruh filter. Tambah `Clear filters` berukuran 44 px, kembalikan fokus ke pencarian, dan pertahankan live updates yang paused. Saat halaman riwayat terakhir menghapus tombol Load older, fokus kembali ke list/table atau pencarian bila hasil kosong; request lambat tidak merebut fokus yang sudah dipindahkan pengguna. | Empat regresi fokus gagal sebelum fix; 45 tes terkait lulus setelah fix. Enam tes komponen baru dan dua tes browser mencakup kedua halaman, kedua tema, reset hasil kosong, fokus dan pause. Chrome 390 px juga mengonfirmasi reset tanpa overflow pada kedua tema. |
| Form pending dan retry dialog | Token login masih bisa diubah dan form dikirim lagi dengan Enter saat request berjalan. Jadikan input read-only selama pending dan abaikan submit tambahan. Retry yang berhasil dalam dialog sebelumnya mencoba memindahkan fokus ke heading halaman di belakang dialog; sekarang kembali ke heading dialog aktif. Jangan mengambil fokus yang sudah dipindahkan pengguna atau memulihkan fokus ke dialog yang ditutup/inert. | Dua tes regresi gagal sebelum fix. Lima tes komponen baru mencakup login tertunda, retry dalam dialog, fokus yang sudah dipindah, dialog ditutup, dan integrasi retry daftar provider di form policy. Tes Playwright baru memakai clock terkendali untuk pending/rejection/login ulang. Chrome 390 px mengonfirmasi read-only saat pending, input kembali editable dan fokus setelah penolakan, kedua tema tanpa overflow, lalu login ulang berhasil. |

SectionNav, target field 44 px, dan spacing Overview dari iterasi sebelumnya ikut diverifikasi.

## Verifikasi integrasi terbaru

Node yang dipakai: `22.23.4`. Node bawaan shell `22.12.0` gagal memulai worker jsdom/undici
pada dependency main terbaru; ini kegagalan toolchain sebelum tes dijalankan.

| Pemeriksaan | Hasil |
| --- | --- |
| Vitest penuh | 110 file, 2.702 tes lulus |
| Layout setelah adaptasi lint React 19 | 23 tes lulus |
| Playwright penuh pada build mock stabil | 61/61 lulus |
| Google Chrome melalui Playwright | 196 kombinasi: 14 route × 7 lebar × 2 tema |
| axe pada 390/1440 px, kedua tema | 56 pemindaian, tanpa pelanggaran terdeteksi |
| Layout matriks | Tanpa overflow halaman, ellipsis terpotong, atau page error |
| ESLint dan TypeScript aplikasi/E2E | Lulus |
| Build produksi dan batas bundle | Lulus; 5 start-up chunks, 370,2 KiB |
| Audit dependency produksi | 0 vulnerabilities |
| gitleaks working tree dan seluruh history | Lulus, tanpa kebocoran terdeteksi |
| Checkout asal | SHA256 seluruh 38 file lokal cocok dengan snapshot awal |

Lebar: 320, 375, 390, 768, 1024, 1280, 1440 px.
Route: Overview, Plans, plan detail, migration detail, Providers, inventory sumber,
Events, Advisor, recommendation detail, Policies, advisor audit, Users, Hooks dan inventory tujuan.
Screenshot Overview tema terang pada 390/1440 px diperiksa secara visual.

Build mock terbaru dilayani di `http://127.0.0.1:4176`; server checkout asal pada port 4175 dipertahankan.
Konektor Chrome pengguna tidak tersedia pada pemeriksaan integrasi terakhir; matriks memakai Google Chrome
terpasang melalui Playwright dalam sesi terpisah. Tidak diperlukan perubahan izin browser lagi.

## Bukti lokal

Artefak berikut berada di mesin pengujian, bukan dependency CI atau lampiran yang tersedia bagi pembaca PR:

- `/tmp/seamless-uiux-pr-tests-final-20261028.log`
- `/tmp/seamless-uiux-pr-layout-final-20261028.log`
- `/tmp/seamless-uiux-pr-e2e-20261028.log`
- `/tmp/seamless-uiux-pr-browser-audit-20261028.log`
- `/tmp/seamless-uiux-pr-browser-matrix-20261028/results.json` dan screenshot pada folder yang sama
- `/tmp/seamless-uiux-pr-{lint-clean,types-final,build,audit}-20261028.log`

Regresi sebelum fix dari iterasi awal tercatat pada log lokal
`/tmp/seamless-uiux-activity-before-20261028.log` dan
`/tmp/seamless-uiux-recovery-before-20261028.log`.
Tes browser kini memakai build stabil dan output tiap run terpisah.

## Review dan batas hasil

Jev aggregate dan review terpisah untuk fokus, pilihan/filter, serta navigasi/tema mengembalikan
`escalate` karena confidence rendah, terutama pada rubric test gap; tidak ada diagnosis bug konkret
pada output tool. Diff dan regresi diperiksa langsung, dan bukti suite/browser dicantumkan untuk review PR.
Gate akhir tanpa truncation juga `escalate`: safe_to_apply `0.51`, composite `0.71`,
limiting rubric `test_gap` (confidence `0.04`). Ketiga klaim bukti berverdict `verified`,
tetapi klaim gabungan Vitest/Layout berconfidence `0.49` sehingga masih perlu review.
Playwright `0.94` dan matriks Chrome `0.92` mendapat action `auto`. Tidak ada klaim contradicted
atau unsupported. Verdict ini tidak dinyatakan sebagai persetujuan otomatis; PR disiapkan sebagai draft
untuk review manusia. Percobaan gate dengan evidence besar sempat terpotong/HTTP 400; hasil di atas
berasal dari payload akhir yang dibatasi, bukan percobaan gagal tersebut.

Hasil membuktikan UI pada fixture lokal. Migrasi, provider, dan layanan backend nyata tidak diuji.
Pemindaian axe bukan bukti kepatuhan WCAG keseluruhan atau kenyamanan semua pengguna.
Tidak ada dependency baru atau perubahan backend/API/auth-provider. Commit dan push hanya untuk PR;
merge dan deploy di luar lingkup.
