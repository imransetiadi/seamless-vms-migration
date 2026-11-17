# Audit dan remediasi UI/UX Seamless Migrate — 17 November 2026

Basis: `main` pada `157a5b51bd6a3287cf670cad3afb884a524802db`, setelah review dan squash merge PR dependensi dashboard [#210](https://github.com/imransetiadi/seamless-vms-migration/pull/210). Implementasi dan gate penerimaan remediasi: [PR #211](https://github.com/imransetiadi/seamless-vms-migration/pull/211), branch `fix/dashboard-uiux-audit-2026-11-17`. Acuan perilaku dan desain: SDD §16, §23.3, §24.3–§24.9 serta design system dashboard yang berjalan.

Audit memeriksa tugas operator, bukan sekadar tampilan: menghubungkan provider, memilih VM, membuat dan mengubah plan, validasi/start/approval/finalize, memantau downtime dan status migrasi, mengelola hooks dan notifikasi, mencari peristiwa dan menelusuri keputusan advisor. Mutasi memakai fixture sintetis; audit tidak mengubah resource cloud nyata.

## Temuan dan remediasi

P2 berarti kehilangan pekerjaan/konteks, feedback keliru, hambatan keyboard atau recovery yang hilang. P3 berarti friksi navigasi atau kurangnya feedback yang tidak memblokir tugas. Tidak ditemukan P0/P1 baru dalam lingkup dan kondisi yang diuji. Tidak ada klaim sertifikasi aksesibilitas atau kesiapan migrasi produksi dari audit UI ini.

| ID | Prioritas | Temuan terverifikasi | Perubahan nyata dan penerimaan |
| --- | --- | --- | --- |
| UX17-01 | P2 | Isi Name di Hooks/Notifications, lalu navigasi: draft hilang tanpa peringatan. Edit item lain juga mengganti draft; Cancel membuang perubahan hook. Probe Chromium kembali ke `/` tanpa alertdialog; tes regresi gagal sebelum perubahan. | Form membandingkan nilai dengan snapshot awal. Navigasi, Back, reload, Cancel dan pergantian item dilindungi; Keep editing mempertahankan nilai, discard eksplisit meneruskan tindakan. Form bersih dan save sukses tidak meminta discard. |
| UX17-02 | P2 | Generate signing key lalu Escape: dialog langsung hilang, key tidak tersedia lagi. Probe Chromium dan tes regresi mereproduksi kehilangan nilai. | Escape/Cancel pada secret yang berubah meminta konfirmasi. Key tetap hanya di memori; discard/save membersihkan dialog. Satu route blocker melindungi form dan secret bersama, sehingga tidak ada blocker bersaing. Input dan generator terkunci selama write berlangsung. |
| UX17-03 | P2 | Setelah channel dihapus, `document.activeElement` menjadi `BODY`. Tombol Remove subscription juga menghilang bersama fokusnya; tes regresi mereproduksinya. | Hapus hook/channel mengembalikan fokus ke heading Catalog. Remove subscription memindahkan fokus ke heading panel jika tombolnya masih memegang fokus; fokus yang sudah dipindahkan pengguna tidak direbut. |
| UX17-04 | P2 | Saat PATCH Enable/Disable ditahan, tombol Edit dan Disable channel lain masih aktif. Beberapa operasi mengganti state mutation dan indikator busy yang sama; tes dengan request tertahan gagal sebelum perbaikan. | Operasi katalog diserialkan di UI. Aksi yang konflik tetap dapat difokuskan dan menjelaskan alasan unavailable. Field yang sedang dikirim tidak bisa diubah; submit lewat Enter memindahkan fokus ke tombol submit sebelum field dikunci, dan tombol tetap focusable selama request. Mutation Enable/Disable dipisahkan dari save form agar kegagalannya tidak ditampilkan sebagai kegagalan penyimpanan draft. |
| UX17-05 | P2 | Kegagalan membaca channel di panel Notifications detail plan membuat Add tidak tersedia tanpa Retry. Saat masih loading, picker menyebut `No channels registered`. Kedua tes regresi gagal sebelum perubahan. | ErrorBanner dan Retry tersedia di samping form; subscription yang sudah terbaca tetap terlihat. Picker membedakan `Loading channels…`, `Channels unavailable` dan katalog kosong. Setelah Retry berhasil, picker dan Add pulih. |
| UX17-06 | P3 | Di phone, form Add channel memenuhi lebih dari satu layar sebelum Catalog, sedangkan status pengiriman dan subscriptions berada jauh di bawah. Screenshot awal 390 × 900 memperlihatkan seluruh viewport dihabiskan form. | Navigasi bagian bernama memberi satu tindakan ke Catalog, form dan Subscriptions. Anchor memakai region yang fokusable dan scroll spacing yang sudah ada. Pergeseran fragment tidak meminta discard atau menghapus draft. |
| UX17-07 | P3 | Register/save/delete katalog mengubah form atau menghapus item tanpa pengumuman hasil sukses yang stabil; operator harus menyimpulkan hasilnya dari perubahan halaman. | Live region yang sudah terpasang mengumumkan register/add/save/delete, enable/disable dan save secret. Subscription add/remove juga memberi feedback. Pesan sukses memakai ikon dan teks, bukan warna saja. |

Implementasi utama: `dashboard/src/pages/Hooks.tsx`, `Notifications.tsx`, `components/Subscriptions.tsx`, `Panel.tsx`, `UnsavedChangesGuard.tsx` dan `lib/useDraftGuard.tsx`. Pola visual, role dan kontrak API dipertahankan; perubahan tidak menambah dependensi.

## Cakupan menyeluruh

| Area | Pemeriksaan |
| --- | --- |
| Login dan sesi | Required token, pending/rejection, tema, font lokal, pergeseran layout saat health tiba, sign-in/out serta batas role. |
| Shell | Sidebar penuh/rail/drawer, label dan current page, identitas/role/demo, skip link, keyboard/focus, tinggi layar pendek, tema tersimpan. |
| Overview | Hierarki downtime/approval/attention, statistik unavailable, chart dan alternatif data, nama panjang, kontras dan reflow. |
| Plans dan detail | Filter/sort/Back, create/edit/discard, VM selection, validate/start, waves, finalize, status, section navigation dan panel Notifications. |
| Migration detail | Next step, approval dan alasan unavailable, strategi, network/GPU/extent/evidence, downtime, history, fokus dan keyboard scroller. |
| Providers dan Inventory | Add/edit/credential dialogs, loading/error/retry, source OpenStack dan VMware, destination inventory, selection dan pembuatan plan. |
| Hooks | Empty catalog, register/edit/delete, immutable identity, error recovery, perlindungan draft, fokus dan navigasi mobile. |
| Notifications | Tiga jenis channel, state/cursor/test outcome, role, draft/edit/delete, secret generation dan discard, busy state dan subscription recovery. |
| Events | Filter/search/Pause, download, empty results, history, reconnect, detail-Back dan error recovery. |
| Advisor | Recommendations, evidence, incident search, service state, policy forms, audit history/pagination, input snapshot dan missing entity. |
| Users dan 404 | Admin-only creation, role feedback, required input, token sekali tampil dan acknowledgement, navigation recovery. |

Bukti berasal dari source review, browser Chromium/WebKit, pengujian unit/integrasi dashboard, suite browser mock dan suite fetch/API sintetis. Audit lama menjadi petunjuk untuk diperiksa ulang, bukan bukti kelulusan saat ini.

## Verifikasi dan artefak

Matriks responsif final mencakup **252 kombinasi**: 18 route/state × 7 lebar (320, 390, 640, 768, 1024, 1280, 1440 px) × 2 tema. Tidak ditemukan overflow halaman atau error JavaScript pada seluruh kombinasi tersebut. **72 scan axe** pada lebar 390/1440 px di kedua tema tidak menemukan pelanggaran pada tag WCAG yang diuji. Hasil mentah disimpan pada [matrix.json](audits/uiux-2026-11-17/matrix.json).

| Sebelum | Setelah |
| --- | --- |
| [Notifications phone](audits/uiux-2026-11-17/notifications-phone-before.png): form menghabiskan layar awal | [Notifications phone](audits/uiux-2026-11-17/notifications-phone-after.png): Catalog dan Subscriptions dapat dicapai dari awal |

Gate penerimaan untuk commit PR tersedia pada [Checks PR #211](https://github.com/imransetiadi/seamless-vms-migration/pull/211/checks). Cakupannya: TypeScript aplikasi/E2E, ESLint, seluruh suite unit, produksi build dan bundle gate, npm production audit, browser mock Chromium/WebKit, serta browser fetch/API/fault-injection Chromium/WebKit. Pemeriksaan PR tidak menjalankan job image dan Compose yang khusus push main. Bukti layout dan axe di atas merupakan hasil terukur; bukti suite menyeluruh adalah hasil job CI pada commit PR tersebut.

Build produksi lokal lulus: **5 startup chunks, 429,7 KiB**, tanpa chart lazy yang masuk startup. Angka startup sama dengan build CI #210 sebelum remediasi ini. `npm audit --omit=dev --audit-level=high` melaporkan **0 vulnerabilities**. Enam regresi unit katalog yang terarah lulus, termasuk keyboard Enter saat request ditahan.

Run lokal awal dijalankan bersamaan dengan pekerjaan berat lain dan menghasilkan timeout pada suite lama serta trace Playwright yang saling terhapus. Hasil gagal tersebut tidak dihitung sebagai kelulusan. Dua selector tes baru diperbaiki agar memakai accessible role/name, dan Back pada secret diuji melalui navigasi aplikasi; Back antar-dokumen memakai peringatan native browser. Pengujian diulang dengan trace terpisah dan gate CI pada commit yang dapat ditinjau. Assertion perilaku tidak dihapus untuk menutup temuan.

Regresi permanen: `pages/CatalogDrafts.test.tsx`, `components/Subscriptions.test.tsx`, `components/UnsavedChangesGuard.test.tsx`, `e2e/catalogs.spec.ts` dan `e2e/api-audit.spec.ts`. Kasus API audit sekarang dijalankan di CI, termasuk 13 skenario load 503/Retry dan pemulihan katalog subscription. Tes clock demo yang semula memakai waktu nyata juga dibuat deterministik: lima detik pada kecepatan 60× wajib terbaca tepat 5:00, sehingga assertion lebih spesifik dan tidak terlewat akibat render yang terlambat. Kedua suite Playwright memakai direktori trace terpisah agar tidak saling menghapus artefak ketika dijalankan bersamaan; trace kegagalan juga diunggah CI. Assertion lama tetap dipertahankan; dua journey unit yang sengaja membuang draft kini melakukan konfirmasi discard secara eksplisit.

Artefak lokal berada di `/tmp/seamless-uiux-20261117/`: `probes.json`, `red.log`, `subscriptions-red.log`, `subscription-focus-red.log`, screenshot sebelum remediasi, log unit/build/browser dan direktori `final/` untuk matriks serta screenshot hasil akhir. Artefak `/tmp` bukan penyimpanan permanen; spesifikasi, tes regresi dan laporan ini disimpan di Git.

## Status CI repositori dan batas audit

Seluruh check yang dijalankan pada PR #210 lulus sebelum merge, termasuk 2.852 tes dashboard, 149 tes browser, backend SQLite/PostgreSQL pada Python 3.11/3.13, collection, deployment, audit dependensi dan CodeQL. Image dan Compose sengaja tidak dijalankan pada pull request; keduanya diperiksa pada push `main`.

[CI pascamerge #210](https://github.com/imransetiadi/seamless-vms-migration/actions/runs/44052424514) kembali menemukan kegagalan image yang sudah ada sebelum perubahan dashboard: **42 HIGH, 0 CRITICAL**, pada `krb5-devel`, `krb5-libs` dan `libkadm5`. Image memasang `1.21.1-10.el9_8.1`; scanner meminta `1.21.1-10.el9_9.2`. Metadata repositori UBI 9 publik yang diperiksa pada 17 November masih menyediakan versi lama meskipun Containerfile sudah menjalankan `dnf update`. Tidak ada advisory yang diabaikan atau ambang scanner yang diturunkan untuk membuat CI hijau. Ini penghalang keamanan image terpisah; laporan ini tidak menyatakan seluruh CI `main` lulus.

Matriks memakai fixture sintetis dan browser automation. Tidak ada pengujian screen reader manusia, pengaturan Safari pada perangkat fisik, user study operator, Core Web Vitals lapangan, instalasi air-gap nyata atau migrasi cloud produksi. Skala/latensi API produksi dan kredensial cloud nyata berada di luar bukti audit ini. Skenario loading/error/pending berasal dari respons sintetis yang sengaja dikendalikan.
