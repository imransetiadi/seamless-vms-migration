# Audit font dashboard — 29 Oktober 2026

## Hasil dan cakupan

Audit tipografi mengacu pada SDD §16 dan design system dashboard. Fira Sans untuk teks,
Fira Code untuk judul, angka dan ID tetap dipertahankan. Temuan ukuran terlalu kecil,
jarak baris rapat dan kegagalan reflow diperbaiki pada branch lokal
`audit/dashboard-fonts-20261029`, berbasis `57b3a059`.

Cakupan source: 158 berkas `.ts`, `.tsx`, `.css` di `dashboard/src`, di luar tes dan
helper tes. Pemeriksaan menelusuri font family, weight, ukuran, line-height, heading,
input, hint/error, tombol, badge, navigasi, tabel, angka, timer, tooltip dan SVG grafik.
Tidak ada perubahan backend, API, auth atau dependensi.

Cakupan tampilan: Login, shell/navigasi, Overview, Plans, Plan Detail, Migration Detail,
Providers, Inventory sumber dan tujuan, Events, Advisor, Recommendation Detail,
Policies, Advisor Audit, Users, Hooks dan halaman tidak ditemukan. Dialog New Plan,
New Policy dan Add Provider diperiksa terpisah.

## Temuan dan perbaikan

| Temuan | Dampak | Perbaikan |
| --- | --- | --- |
| Navigasi rail/status dan tanggal aktivitas memakai 10–11px | Label kecil dan tidak mengikuti preferensi root font | Pakai `text-xs` / `0.75rem` |
| Teks padat `text-xs` dan `text-sm` memakai jarak baris sempit | Hint dan paragraf multibaris sulit dipindai | Token line-height keduanya `1.5`; pengecualian ringkas untuk navigasi dan display angka |
| Label SVG grafik memakai `fontSize: 12` | Label tetap 12px ketika root font menjadi 200% | `0.75rem` pada ketiga grafik; ruang sumbu ditambah |
| Grafik terpotong pada lebar kecil atau teks besar | Sebagian label hilang | Gunakan `ScrollRegion` yang sudah ada, nama aksesibel, fokus keyboard dan lebar minimum `24rem`; alternatif tabel tetap ada |
| Header, metrik, timer, fase, label tombol dan badge tidak cukup fleksibel | Overflow pada 320px dengan teks 200% | Wrapping dan batas lebar; header/action row boleh pindah baris |
| Grid metadata Providers/Policies dan lifecycle migrasi terlalu kaku | Kolom nilai menyempit hingga nol atau keluar viewport | Kolom `minmax(0,…)`, item bisa menyusut dan label membungkus |
| Evidence dan tab detail rekomendasi melebar saat teks besar | Bagian detail keluar viewport | Evidence satu kolom pada layar kecil; tab membungkus |
| Wrapping diwariskan ke tabel data | Tabel NIC kehilangan kebutuhan scroll horizontal | `wrap-normal` pada tabel; scroll keyboard dan pengelompokan data dipertahankan |
| Fira Sans 700 diimpor tetapi tidak digunakan | Aset font berlebih pada build | Hapus satu import; pertahankan 400/500/600 yang digunakan |

Perubahan jarak baris sempat menggeser downtime clock pertama dari layar awal.
Padding ringkasan ponsel disesuaikan; ukuran font dipertahankan. Kontrak SDD §16
untuk clock dan SLO di layar pertama 390×844 tetap menjadi tes regresi.

## Konvensi yang diperiksa

- Body: Fira Sans, umumnya `0.875rem` untuk dashboard padat; input `1rem` di bawah
  640px dan `0.875rem` mulai 640px. Root mengikuti ukuran default browser.
- Judul halaman: Fira Code 600, `1.25rem` ponsel / `1.5rem` desktop.
  Judul panel `0.875rem`; hint/tanggal/navigasi/grafik minimal `0.75rem`.
- Weight: 400, 500 dan 600. Angka memakai tabular numerals melalui `.num`.
  Penekanan `<strong>` eksplisit memakai 500/600; tidak bergantung pada font 700 sintetis.
- Line-height teks kecil/padat `1.5`. Display angka dan label navigasi ringkas dapat
  memakai 1–1.25; override jarak teks pengguna diperiksa terpisah.
- Font lokal `@fontsource`, `font-display: swap`, dengan fallback sistem. Tidak ada
  Google Fonts atau font lintas origin. Latin mengikuti kontrak SDD; karakter lain
  bergantung pada fallback sistem.
- Tidak ada ukuran font piksel atau ellipsis/clamp pemotong teks yang ditemukan
  dalam source UI setelah perbaikan. Area tabel/grafik boleh scroll secara lokal.

`0.75rem` adalah batas desain dashboard ini, **bukan** ukuran minimum yang diwajibkan
WCAG. Penilaian aksesibilitas juga mencakup kontras, reflow, pembesaran teks,
spacing dan akses keyboard. Audit ini bukan sertifikasi WCAG menyeluruh.

## Bukti verifikasi

Validasi akhir dicatat setelah build mock stabil pada `http://127.0.0.1:4177`.

| Pemeriksaan | Hasil |
| --- | --- |
| Lint, typecheck, production build | Lulus; startup bundle 5 chunk / 370.2 KiB |
| Full Playwright Chromium, build akhir | 65/65 lulus; termasuk 4 regresi tipografi, scroll NIC dan clock/SLO di layar pertama |
| Vitest lengkap | 110 file / 2.702 tes lulus sebelum perbaikan wrapping terakhir |
| Vitest komponen/halaman terdampak setelah wrapping terakhir | 6 file / 186 tes lulus |
| Google Chrome 156.0.8078.73 | 270 kombinasi / 15 rute / 2 tema; 0 overflow halaman, 0 teks terlihat di bawah 0.75rem, 0 clipping yang terdeteksi, 0 page error |
| Axe pada lebar 768 dan 1440 | 60 scan, 0 pelanggaran WCAG A/AA yang terdeteksi |
| Login dan 3 dialog utama, 320px / root 200% | 0 overflow halaman; 0 overflow horizontal dialog |
| Font aktual via Chrome DevTools Protocol | FiraSans-Regular pada deskripsi; FiraCodeLight-SemiBold pada H1 dan metrik |
| Font network/fallback | Hanya origin preview; 0 custom font face berstatus loaded dalam skenario fallback |

Matriks Chrome memakai 9 kondisi per rute/tema: normal pada 320/768/1440;
root font 200% pada 320/1440; text spacing pada 320/1440; font diblokir pada
320/1440. Spacing override: line-height 1.5, letter-spacing 0.12em,
word-spacing 0.16em, margin bawah paragraf 2em. Probe geometri mengabaikan teks
screen reader yang memang tersembunyi dan elemen pengukur internal Recharts.

Artefak lokal sesi ini (folder `/tmp` tidak menjadi arsip permanen):

- `/tmp/seamless-font-audit-accepted-20261029/results.json`: matriks, computed style, font faces, axe.
- `/tmp/seamless-font-browser-accepted-20261029.log`: full Playwright akhir.
- `/tmp/seamless-font-tests-final-20261029.log`: full Vitest.
- `/tmp/seamless-font-latest-components-20261029.log`: Vitest komponen setelah wrapping terakhir.
- `/tmp/seamless-font-actual-20261029.json`: platform fonts dan geometri dialog.
- `/tmp/seamless-font-audit-accepted-20261029/`: screenshot Overview/Events dan kondisi resize/spacing/fallback.


Jev advisory review dijalankan pada diff dan bukti nyata. Hasil tetap
`review/escalate` dengan confidence rendah; tidak ada persetujuan otomatis.
Hasil itu bukan bukti lulus/gagal browser. Bukti langsung di atas tetap dicatat
beserta batasnya; audit ini tidak mengklaim review patch Jev sudah disetujui. Verifikasi terpisah
atas output 65 tes Playwright, 186 tes Vitest dan 270 kombinasi Chrome kemudian
mendapat `verified/auto` untuk ketiganya.

Tes regresi permanen: `dashboard/e2e/typography.spec.ts` mencakup ukuran label,
jarak baris, label grafik pada 200% dengan scroll keyboard, dan 10 rute pada
320px/200%. Tes diamati gagal sebelum perbaikan dan lulus setelahnya.

Command utama dari `dashboard/`:

```sh
npm test
npm run lint
npm run typecheck
npm run build
npm run test:e2e
```

E2E membangun aplikasi dengan fixture mock. Untuk preview audit ini, config sementara
menargetkan port 4177, dua worker, tanpa membangun ulang preview selama browser berjalan.
Setiap run memakai direktori artefak terpisah.

## Batas dan tindak lanjut

- Data browser adalah fixture lokal; backend produksi dan data pelanggan tidak diuji.
- Root font 200% adalah uji resize teks; bukan pengganti uji zoom browser dan semua
  konfigurasi pembesaran OS. Perangkat fisik, Safari dan Firefox belum diuji dalam audit ini.
- Axe dan pemeriksaan geometri tidak membuktikan kenyamanan visual untuk semua pengguna,
  kualitas screen reader, atau ketiadaan benturan pada seluruh isi dinamis.
- Tabel/grafik lebar menggunakan scroll lokal, dengan region bernama dan alternatif tabel
  pada grafik. Teks label/nilai tidak disembunyikan dengan ellipsis.
- Perubahan belum di-commit, push atau deploy. Checkout utama dan 38 berkas perubahan
  sebelumnya diverifikasi tetap utuh melalui SHA-256.
