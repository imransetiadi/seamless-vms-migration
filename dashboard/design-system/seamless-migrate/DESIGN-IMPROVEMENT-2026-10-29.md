# Perapian desain dashboard — 29 Oktober 2026

Perubahan UI lokal pada `audit/dashboard-fonts-20261029`, melanjutkan audit font.
Kontrak SDD §16, logo S yang sudah ada, Fira Sans/Fira Code, dan palet terang lembut dipertahankan.

| Area | Hasil |
| --- | --- |
| Logo | Mark shell 32px dengan jarak wordmark 12px; login 48px dengan jarak 16px. Blok login membungkus saat teks diperbesar; SVG dan identitas tetap. |
| Header halaman | Judul, konteks, filter dan aksi dikelompokkan dalam kartu; padding 12px ponsel / 20px layar lebih besar. |
| Kartu | Radius 12px, bayangan tipis, header panel bertint lembut. Kartu statis tidak bergerak saat hover. |
| CTA | Tombol dan segmented control minimal 44px di bawah 640px; aksi utama tetap 44px. Ringkasan Plans punya View all plans. |
| Kartu utama | Downtime now diberi aksen informasi; label dan ikon status tetap menyampaikan kondisi VM. Copy berulang diringkas. |
| Kolom Overview | Rasio operasional 3:2 mulai 1024px; di bawahnya clocks, approvals, attention, lalu history mengikuti urutan DOM. |
| Tabel Plans | Status berada di bawah nama sebelum 1024px; desktop memakai kolom minimum 8rem. Nama, status dan progress lebih lapang. |
| Providers | Jarak kartu 16px, padding 16/20px, footer aksi dipisahkan border dan padding. |

Semua angka dan hint metrik tetap tampil. Clock dan SLO pertama berada hingga y=835px
pada layar 390×844 dalam kedua tema. Aturan penggunaan komponen diperbarui di
`MASTER.md` dan `pages/dashboard.md`; template marketing yang tidak sesuai dashboard dihapus.

## Verifikasi

Validasi memakai build mock stabil pada `http://127.0.0.1:4177`, Node 22.23.4.
- Vitest lengkap: 110 file / 2.702 tes lulus sebelum pembungkusan logo login terakhir;
  seluruh 10 tes Login diulang dan lulus setelahnya.
- Lint, typecheck, production build dan bundle check lulus setelah perbaikan login;
  startup bundle 5 chunk / 370,3 KiB.
- Full Playwright Chromium: 67/67 tes lulus setelah perbaikan Login, termasuk note tombol di tepi viewport,
  clock/SLO pada layar awal, reflow, fokus, role pengguna dan alur operasional.
- Regresi layout Plans dan CTA: 4 tes lulus, termasuk 320/768/1280px,
  progress bar minimal 40px dan isi sel tidak meluber.
- Regresi permanen baru: `dashboard/e2e/design.spec.ts`, mencakup navigasi Plans,
  CTA 44px, label Running satu baris pada 320/390/768px, dan logo login pada teks 200%.
  Tes logo diamati gagal sebelum perbaikan lalu lulus sesudahnya.
- Login Chrome 320px, dua tema, teks 100/200%: judul 1/2 baris, tanpa overflow.
- Matriks Google Chrome 156.0.8078.73: 390 kombinasi, 15 rute signed-in, dua tema;
  normal pada 320/375/390/768/1024/1280/1440px, root font 200%, text spacing dan
  font diblokir pada 320/1440px. Tidak terdeteksi overflow halaman, clipping atau
  page error; 60 scan axe tanpa pelanggaran A/AA yang terdeteksi. Font hanya dari
  origin preview; tidak ada custom font loaded saat font diblokir.

Matriks 390 kombinasi dijalankan sebelum perubahan terakhir yang hanya menyentuh
blok logo Login. Command matriks berakhir exit 1 karena satu pengukuran Recommendation
Detail membaca sebagian font pada ukuran lama setelah root diubah menjadi 200%.
Itu tidak dihitung sebagai run seluruhnya hijau. Uji terarah 20 kombinasi pada
320/1440px, dua tema dan lima pengulangan, menunggu seluruh computed font size
sesuai root sebelum mengukur: lulus tanpa teks kecil, overflow atau clipping.
Tidak ada perubahan source font untuk mengatasi pengukuran ini.

Iterasi sempat mempersempit progress bar pada 768px; pemindahan status ke bawah nama
sampai breakpoint 1024px memperbaikinya tanpa menghapus informasi. Satu run Vitest
sebelumnya gagal pada penyimpanan hook; full rerun tanpa proses browser bersamaan
lulus seluruh 2.702 tes tanpa perubahan source Hooks. Satu tes note tombol sempat
hilang sesaat setelah muncul; penyebab belum terbukti. Pengulangan 5 kali dan full
rerun sebelum perapian tabel lulus, tanpa mengubah perilaku dismissal.

## Bukti lokal dan batas

- Preview: `http://127.0.0.1:4177`.
- Vitest: `/tmp/seamless-design-vitest-stable-20261029.log`.
- Browser akhir: `/tmp/seamless-design-final67-browser-20261029.log`.
- Matriks Chrome: `/tmp/seamless-design-matrix-accepted-20261029/results.json`.
- Pengulangan font: `/tmp/seamless-design-matrix-rem-focused-20261029/results.json`.
- Lint/typecheck/build dan tes Login: `/tmp/seamless-design-logo-checks-20261029.log`.
- Screenshot pembanding: `/tmp/seamless-design-before-20261029/` dan
  `/tmp/seamless-design-final-20261029/`. Artefak `/tmp` bukan arsip permanen.

Jev advisory review pada diff menghasilkan `escalate`, confidence correctness dan
safe-to-apply 0.29; patch tidak mendapat persetujuan otomatis. Hasil pemeriksaan
langsung dicatat terpisah, bukan sebagai persetujuan Jev atau sertifikasi WCAG.
Verifikasi klaim terpisah atas output Vitest dan lint/typecheck/build mendapat
`verified/auto`. Verifikasi output 67 tes Playwright, 10 tes Login, serta hasil
matriks beserta batasnya juga mendapat `verified/auto`.
Review terpisah atas pembungkusan logo Login juga menghasilkan `escalate`, kali ini
karena confidence test-gap 0.47; correctness 0.86 dan safe-to-apply 0.87. Tidak ada
persetujuan otomatis atas patch Login.

Browser memakai fixture lokal. Safari, Firefox, perangkat fisik, screen reader dan
backend produksi tidak diuji. Root font 200% bukan pengganti seluruh pengujian zoom
browser/OS. Axe dan probe geometri hanya mendeteksi jenis masalah yang diperiksa.
Tidak ada perubahan backend, API, auth, dependensi, commit, push atau deploy.
38 berkas perubahan lama pada checkout utama diverifikasi tetap utuh dengan SHA-256.
