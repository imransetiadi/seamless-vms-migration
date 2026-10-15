# AUDIT UI/UX KOMPREHENSIF DASHBOARD SEAMLESS MIGRATE
**Disusun oleh:** Agentic Gemini (AI Contributor & Systems Auditor)  
**Tanggal Audit:** 15 Oktober 2026  
**Target Aplikasi:** Dashboard Web Seamless Migrate (Operator UI untuk Migrasi VM ke RHOSO 18.0)  
**Basis Kode:** Branch `main` (commit `239009bf3020b87fa6b4550a7bff4eb6b825dd42`), menyertakan modul Users (PR #10)  
**Spesifikasi Rujukan:** `docs/SDD.md` Bagian 16, `docs/PRD.md` FR-18, `dashboard/design-system/seamless-migrate/MASTER.md`

---

## 1. RINGKASAN EKSEKUTIF

Audit UI/UX komprehensif ini mengevaluasi seluruh antarmuka web (*dashboard tools*) pada sistem **Seamless Migrate**, platform orkestrasi migrasi mesin virtual dari RHOSP 17.1 / OpenStack Community / VMware vSphere ke Red Hat OpenStack Services on OpenShift (RHOSO) 18.0.

Dashboard ini dibangun menggunakan arsitektur antarmuka modern: **Vite 8 + React 18 + TypeScript 5 (strict) + Tailwind CSS 4.3 (CSS-first theme)**, dengan sistem desain *"Minimalism & Swiss Style"* (densitas 8/10, gerak 3/10).

### Nilai Evaluasi Kesiapan Antarmuka (UX Readiness Score)
* **Kesesuaian Desain & Identitas Sistem:** 9.2 / 10 (Sangat Kuat)
* **Aksesibilitas (WCAG 2.2 AA Compliance):** 8.8 / 10 (Baik, ada perbaikan minor pada scroller focus)
* **Ergonomi Operasional & Safety Gates:** 9.5 / 10 (Sangat Baik, maker-checker dan modal ketik nama VM sangat protektif)
* **Respon Layar Mobile & Tablet (320px – 768px):** 8.3 / 10 (Cukup, ditemukan kompresi layout pada beberapa kartu metadata)
* **Resiliensi & Error Recovery:** 8.0 / 10 (Perlu penambahan React Error Boundary pada tingkat route lazily-loaded)

Secara menyeluruh, dashboard ini menunjukkan kedewasaan teknis luar biasa untuk perangkat lunak kelas enterprise / infrastruktur misi kritis. Tidak ditemukan cacat level P0 (data loss atau security bypass pada UI). Ditemukan **1 temuan P1**, **9 temuan P2**, dan **4 peluang optimasi P3**.

---

## 2. METODOLOGI & BATASAN AUDIT

### 2.1 Matriks Uji Tampilan
Audit dilakukan secara analitis terhadap kode sumber komponen (`dashboard/src/components/*`), halaman (`dashboard/src/pages/*`), sistem tema (`dashboard/src/theme/*`), serta verifikasi visual pada 4 kelompok viewport:
1. **Mobile Ultra-Compact (320px x 800px):** Batas ekstrem WCAG 1.4.10 Reflow.
2. **Mobile Standar (390px x 844px):** Representasi perangkat smartphone modern.
3. **Tablet / Medium Screen (768px x 1024px):** Transisi antara drawer mobile dan sidebar rail (5rem).
4. **Desktop / Ultrawide (1280px & 1440px x 900px):** Antarmuka penuh dengan sidebar 15rem.

### 2.2 Matriks Tema & Peran Pengguna (RBAC)
* **Tema:** Dark Theme (Latar `#0F172A`, Kartu `#1B2336`, Aksen `#22C55E`) dan Light Theme (Latar `#F8FAFC`, Kartu `#FFFFFF`, Aksen `#EA580C`).
* **Peran:** Viewer, Operator, Approver, Admin.

---

## 3. DAFTAR TEMUAN PRIORITAS (ACTIONABLE FINDINGS)

| ID | Prioritas | Kategori | Komponen / Lokasi | Deskripsi Masalah | Rekomendasi Solusi |
|---|---|---|---|---|---|
| **UI-01** | **P1** | Resiliensi / Crash | `dashboard/src/App.tsx:26` | Chunk file lazily-loaded yang gagal dimuat (misal karena jaringan tidak stabil atau pembaruan deployment) menyebabkan layar putih kosong (*blank screen*) tanpa opsi reload atau penanganan ramah bagi operator. | Bungkus `<Suspense>` dengan React `<ErrorBoundary>` pada tingkat page wrapper. Tampilkan pesan kesalahan bersahabat dengan tombol *"Muat Ulang Halaman"* dan pertahankan navigasi samping. |
| **UI-02** | **P2** | Aksesibilitas (WCAG 2.1.1) | `NetworkPanel.tsx`, `RecommendationDetail.tsx` | Area container yang memiliki scrolling horizontal/vertikal (tabel antarmuka jaringan dan kotak raw input hash) belum memiliki `tabIndex={0}` atau pembungkus `ScrollRegion`, sehingga pengguna keyboard pada browser WebKit/Firefox sulit menjangkaunya. | Manfaatkan komponen `ScrollRegion` yang sudah ada untuk semua tabel dan view JSON/hash yang memiliki potensi overflow. |
| **UI-03** | **P2** | Visual / Ergonomi | `components/Term.tsx:21` | Tooltip istilah teknis (seperti definisi Jev confidence) ditempatkan statis `rect.bottom + 6`. Jika elemen berada di dekat batas bawah layar pada viewport kecil, tooltip terpotong keluar dari viewport. | Tambahkan logika deteksi batas viewport: jika jarak bawah kurang dari tinggi tooltip (misal < 100px), balikkan posisi tooltip ke arah atas (*flip top*). |
| **UI-04** | **P2** | Responsive / Tipografi | `components/EvidenceCard.tsx:45` | Pada viewport 320px, elemen pendukung bukti rekomendasi AI tertekan oleh baris flex sehingga lebar teks menyempit menjadi ~55px dan kata-kata terpotong vertikal. | Ubah tata letak flex menjadi kolom (`flex-col`) pada breakpoint mobile bawah (`sm:flex-row`), berikan nilai teks lebar penuh agar tidak terpotong kata per kata. |
| **UI-05** | **P2** | Responsive / Visual | `pages/AdvisorPolicies.tsx:63` | Judul kebijakan (misal *"Built-in default"*) terdesak oleh lencana (*badge*) status dalam satu baris sempit pada 320px, menyebabkan pemotongan kata ekstrem. | Pindahkan lencana ke baris baru di bawah judul pada layar di bawah 640px (`flex-col sm:flex-row`). |
| **UI-06** | **P2** | Navigasi & State | `pages/Plans.tsx:63` | Filter pencarian dan penyaringan status pada daftar rencana migrasi hanya tersimpan pada state lokal React. Menekan tombol *Back* dari halaman detail menyebabkan input pencarian hilang. | Sinkronkan parameter pencarian, status, dan sorting ke dalam URL Search Parameters (`useSearchParams`), sehingga state bertahan saat navigasi bolak-balik. |
| **UI-07** | **P2** | Pencegahan Kehilangan Data | `components/PlanCreateDialog.tsx` | Menekan tombol `Escape` pada dialog wizard pembuatan plan langsung menutup dialog dan menghapus konfigurasi form yang panjang tanpa konfirmasi perubahan (*unsaved changes guard*). | Pasang proteksi *dirty check*: jika form sudah diisi sebagian, tampilkan konfirmasi *"Batalkan perubahan?"* sebelum menutup modal. |
| **UI-08** | **P2** | Konteks Sesi & RBAC | `components/Layout.tsx` | Pada tampilan rail (768px-1279px) dan mobile header (<768px), informasi peran aktif pengguna dan status lingkungan Demo tidak tampak langsung kecuali drawer dibuka. | Tampilkan indikator status ringkas (*chip badge*) berisi peran pengguna (misal: "Admin") dan status Demo di bilah atas (*top bar*) pada layar compact. |
| **UI-09** | **P2** | Pengelolaan Akses | `pages/Users.tsx` | Modul manajemen pengguna yang baru ditambahkan belum menyediakan fitur revoke/hapus token langsung melalui dashboard, mengharuskan admin menggunakan CLI atau akses database langsung. | Tambahkan aksi hapus/revokasi token pengguna pada baris tabel pengguna dengan dialog konfirmasi keamanan. |
| **UI-10** | **P2** | Ergonomi Konfirmasi | `components/ConfirmDialog.tsx` | Dialog konfirmasi destruktif (Cutover/Finalize) mengharuskan pengetikan nama VM/Plan. Teks error atau petunjuk pencocokan hanya muncul setelah selesai mengetik. | Tambahkan *real-time visual match indicator* yang jelas dan halus ketika karakter yang diketikkan pengguna sudah cocok persis dengan kata kunci verifikasi. |
| **UI-11** | **P3** | Kinerja DOM Mobile | `pages/Events.tsx` | Aliran event audit hingga 1.000 live items menghasilkan tinggi halaman mobile yang sangat panjang (>25.000px). | Pertimbangkan penambahan pagination lokal atau batas buffer awal yang lebih ramping pada perangkat mobile (misal 50 event pertama dengan tombol *"Muat lebih banyak"*). |
| **UI-12** | **P3** | Navigasi Halaman Panjang | `pages/PlanDetail.tsx`, `pages/MigrationDetail.tsx` | Halaman detail migrasi memiliki banyak seksi vertikal (Phase Stepper, Downtime, Network, Probes, Evidence, Timeline). | Sediakan tautan lompatan cepat (*sticky anchor navigation*) di sisi atas untuk memudahkan operator berpindah antar seksi tanpa scroll panjang. |
| **UI-13** | **P3** | Bahasa & Copywriting | Seluruh Tampilan | Penggunaan kode klausul spesifikasi teknis (seperti *"SDD §16"*, *"ADV-19"*) di kartu utama dapat membingungkan operator umum. | Sembunyikan kode klausul teknis ke dalam tooltip info atau dokumentasi bantuan sekunder, dan gunakan bahasa instruksional bisnis yang murni pada deskripsi utama. |
| **UI-14** | **P3** | Status Visual Kosong | `components/EmptyState.tsx` | Ilustrasi keadaan kosong (*empty state*) masih sangat minimalis. | Berikan panduan kontekstual langkah berikutnya (*next actionable step*) yang lebih menonjol dengan tombol tindakan primer langsung di kartu kosong tersebut. |

---

## 4. AUDIT MENYELURUH PER TOOL / WEBSITE PAGE

### 4.1 Global Shell & Navigasi (`components/Layout.tsx`)
* **Struktur Informasi:** Navigasi dikelompokkan dengan sangat rapi ke dalam 4 domain operasional:
  1. *Migrations* (Overview `/`, Plans `/plans`)
  2. *Clouds* (Providers `/providers`, Inventory `/inventory`)
  3. *Activity* (Events `/events`)
  4. *Advisor* (Recommendations `/advisor`, Policies `/advisor/policies`, Audit trail `/advisor/audit`)
  5. Ditambah modul *Users* (`/users`) untuk peran Admin.
* **Perilaku Responsif:**
  - **>= 1280px:** Sidebar penuh (15rem) menampilkan nama brand, ikon, label menu, badge kegagalan, dan sesi akun lengkap di bagian bawah.
  - **768px – 1279px:** Mode rail (5rem) mempertahankan aksesibilitas dengan meletakkan label teks di bawah ikon (bukan tooltip hover), sehingga patuh WCAG tanpa ketergantungan mouse hover.
  - **< 768px:** Bilah header ringkas dengan tombol hamburger yang memicu drawer modal kiri lengkap dengan latar belakang redup (*backdrop blur*), fokus keyboard terperangkap rapi, dan tombol Esc untuk menutup.
* **Keunggulan UX:**
  - Tautan *"Skip to main content"* tersedia di urutan pertama DOM untuk pembaca layar.
  - Terdapat indikator real-time *SSE Live Indicator* yang memberikan umpan balik visual jelas: hijau saat tersambung (*Live*), kuning saat menyambung kembali (*Reconnecting*), dan abu-abu saat berhenti.
* **Area Perbaikan:**
  - Pada mode rail dan mobile, status identitas login dan status mode *Demo* tersembunyi di dalam menu laci. Operator disarankan tetap dapat melihat sekilas siapa peran mereka saat ini melalui header atas.

---

### 4.2 Overview Dashboard (`pages/Overview.tsx`)
* **Tujuan Pengguna:** Memberikan visibilitas instan atas kesehatan migrasi seluruh kluster, batas waktu henti (*downtime SLO*), migrasi yang membutuhkan perhatian manusia (*human attention*), serta metrik throughput.
* **Hierarki Visual:**
  1. Baris KPI Tiles di bagian atas: *Migrations*, *Awaiting Finalize*, *Finalized*, *Failed*, *Rolled back*, *Average downtime*, *SLO compliance*.
  2. Panel *Downtime Now*: Jam hitung mundur waktu henti aktif dengan aksen visual yang berubah jika mendekati atau melampaui SLO (80% amber, >100% merah).
  3. Panel *Pending Approvals* & *Needs Attention*: Mengumpulkan tindakan persetujuan cutover dan keputusan AI Advisor di satu tempat.
  4. Grafik *Downtime vs SLO*, *Throughput 60 menit*, dan *Phase Distribution*.
* **Evaluasi UX & Safety:**
  - Sangat baik: Migrasi yang berstatus *rolled back* secara eksplisit ditampilkan sebagai perhatian operasional (latar peringatan kuning), tidak disamarkan sebagai sukses atau bersih.
  - Penanganan data kosong (*failed load*) tidak menampilkan angka 0 palsu, melainkan tanda *Unavailable* dengan tombol *Retry*, mencegah misinterpretasi operator.

---

### 4.3 Plans Management Tool (`pages/Plans.tsx`)
* **Tujuan Pengguna:** Membuat, meninjau, mencari, menyaring, dan mengurutkan rencana migrasi batch VM.
* **Elemen Interaktif:**
  - Tombol aksi primer *"New plan"* (44x44px, peran Operator/Admin).
  - Kolom pencarian teks dan filter status rencana (*Draft, Validated, Running, Paused, Completed, Finalized, Failed, Cancelled*).
  - Tabel interaktif dengan pengurutan kolom (*Plan name, Status, Clouds, VMs count, Progress bar, SLO, Last Updated*).
* **Evaluasi UX:**
  - Bilah progres Done/Total menguraikan migrasi yang dibatalkan secara cerdas tanpa merusak skala persentase.
  - Pada layar mobile (<640px), kolom sekunder disembunyikan dan diringkas ke dalam baris sub-teks di bawah nama rencana, mencegah *horizontal table crushing*.
  - **Friction Point:** Input filter pencarian hilang saat operator membuka detail rencana lalu menekan tombol *Back*. Ini perlu diperbaiki dengan `useSearchParams`.

---

### 4.4 Plan Detail & Orchestration Tool (`pages/PlanDetail.tsx`)
* **Tujuan Pengguna:** Orkestrasi eksekusi migrasi skala besar: validasi pre-flight, pembuatan wave otomatis, start/pause, finalisasi masal, serta penanganan peringatan probe aplikasi.
* **Fitur Utama:**
  - *Action Bar:* Tombol Validate, Start, Pause, Auto-waves, Edit plan, Finalize wave, Cancel plan.
  - *Waves Board:* Papan alur pembagian gelombang migrasi per batch VM.
  - *Database VM Probe Warning:* Kartu notifikasi khusus untuk database VM tanpa health probe aplikasi, disertai tombol praktis *"Use suggested probe"*.
  - *Finalize Queue:* Antrean VM yang sudah diverifikasi dan menunggu pembersihan VM sumber di cloud lama.
* **Evaluasi Keselamatan Operasional:**
  - Tombol *Cancel Plan* dan *Finalize All* dilindungi oleh `ConfirmDialog` dengan persyaratan mengetik ulang nama rencana secara tepat.
  - Tombol aksi yang tidak valid (misal: Start saat ada migrasi terblokir) tetap dapat difokuskan oleh keyboard dan menjelaskan alasan non-aktifnya saat diklik/di-hover (*accessible disabled state*).

---

### 4.5 Migration Detail & Real-Time Monitor (`pages/MigrationDetail.tsx`)
* **Tujuan Pengguna:** Pemantauan real-time status satu mesin virtual secara mendalam saat proses migrasi berjalan.
* **Fitur Utama:**
  - *Phase Stepper:* Visualisasi langkah migrasi (*Pending -> Pre-copy -> Syncing -> Cutover -> Verifying -> Completed / Finalized*).
  - *Progress Panel:* Menampilkan volume data tertransfer, persentase disk, dan kecepatan streaming transfer.
  - *Sync-Pass Convergence Chart:* Grafik batang pass sinkronisasi berulang hingga selisih data mencukupi kriteria cutover.
  - *Network & Override Panel:* Tabel pemetaan kartu jaringan (NIC), MAC address, IP tetap, dan penyesuaian subnet di RHOSO.
  - *Verification Panel:* Status uji coba boot VM di RHOSO, latensi port aplikasi (SSH, RDP, HTTP), dan hasil health check.
  - *Alerts Divergence:* Peringatan darurat jika VM sumber di cloud lama tiba-tiba dinyalakan kembali saat cutover telah selesai.
* **Evaluasi UX:**
  - Luar biasa: Informasi terperinci namun tetap tertata logis. Perubahan fasa diumumkan ke pembaca layar menggunakan `aria-live="polite"` setiap kelipatan 10% agar tidak menimbulkan kejenuhan suara (*audio spam*).

---

### 4.6 Clouds & Providers Management (`pages/Providers.tsx`)
* **Tujuan Pengguna:** Registrasi dan monitoring kluster OpenStack (Community, Kolla, RHOSP 17.1, RHOSO 18.0) dan VMware vCenter.
* **Fitur Utama:**
  - Kartu ringkasan cloud dengan status konektivitas, kapabilitas storage backend (Ceph RBD, NetApp, NFS, LVM), dan versi API.
  - Tindakan *"Check all"* untuk menguji konektivitas cloud secara paralel dengan pelaporan kegagalan spesifik per item.
  - Wizard penambahan provider dengan preset distribusi, konfigurasi kredensial aman (*write-only*, tidak pernah ditampilkan kembali), dan SSH key host konversi.
* **Evaluasi UX & Keamanan:**
  - Penghapusan provider yang sedang digunakan oleh rencana aktif diblokir langsung di antarmuka dengan penjelasan transparan mengenai rencana apa saja yang sedang mengikatnya.

---

### 4.7 Clouds & Inventory Browser (`pages/Inventory.tsx`)
* **Tujuan Pengguna:** Eksplorasi katalog inventaris VM dari cloud sumber dan pemetaan kapasitas infrastruktur di cloud target (jaringan, flavor CPU/RAM, tipe volume Cinder, kuota per project).
* **Fitur Utama:**
  - Selektor penyedia cloud instan (*in-place provider switch*).
  - Filter pencarian nama VM, status power, sistem operasi (Linux / Windows / Legacy), serta status kepemilikan oleh rencana (*Held in plan* vs *Free*).
  - Pemilihan VM jamak (*multi-select checkbox*) untuk langsung membuat rencana migrasi baru (*"Create plan with N VMs"*).
* **Evaluasi UX:**
  - Operator dapat memilih VM yang belum terikat rencana migrasi dengan cepat dan aman tanpa risiko duplikasi pekerjaan.

---

### 4.8 Activity & Audit Trail (`pages/Events.tsx`)
* **Tujuan Pengguna:** Log audit terpadu atas setiap kejadian sistem, aksi operator, persetujuan, dan perubahan status migrasi.
* **Fitur Utama:**
  - Aliran audit langsung (*live streaming SSE*) dengan filter kategori (*Migrations, Plans and waves, Advisor*), filter rencana spesifik, dan pencarian teks.
  - Tombol kontrol *Pause / Resume Live Updates* yang ramah aksesibilitas.
  - Opsi ekspor data ganda: *Download shown events* (JSONL) dan *Export whole audit trail* (berkas lengkap dari server).
* **Evaluasi UX:**
  - Mengikuti standar keamanan audit enterprise. Pause live stream mencegah pergeseran baris saat operator sedang membaca catatan insiden tertentu.

---

### 4.9 Advisor & AI Recommendations (`pages/Advisor.tsx`, `RecommendationDetail.tsx`)
* **Tujuan Pengguna:** Asistensi berbasis kecerdasan buatan (Jev AI & AgentMemory) dalam memilih strategi migrasi terbaik (*Cold, Warm, Storage Handover, VMware CBT*) berdasarkan profil disk dan beban kerja VM.
* **Fitur Utama:**
  - Kartu status layanan AI: Status koneksi Jev, latensi respon, dan lokasi eksekusi model (*On-prem* vs *Remote endpoint*).
  - Umpan rekomendasi dengan nilai keyakinan yang terkalibrasi (*confidence score*), aturan kebijakan yang diterapkan, dan status persetujuan.
  - Pencarian insiden serupa (*Similar Incidents search*) memanfaatkan memori vektor.
  - Panel transparansi data (*Data Sent to Advisor*) yang menjelaskan secara rinci parameter apa saja yang dikirimkan ke model AI untuk menjaga privasi.
* **Evaluasi UX:**
  - Pendekatan *"Assisted, Never Autonomous"* dieksekusi dengan luar biasa: AI tidak pernah mengambil keputusan sepihak yang merusak; operator selalu memiliki kendali untuk melakukan *Override* dengan pencatatan alasan resmi ke audit trail.

---

### 4.10 Advisor Policies & Audit (`pages/AdvisorPolicies.tsx`, `AdvisorAudit.tsx`)
* **Tujuan Pengguna:** Pengaturan ambang batas otomatisasi AI dan audit historis khusus keputusan berbasis rekomendasi.
* **Fitur Utama:**
  - Daftar hierarki kebijakan: Kebijakan global bawaan (*Built-in default*) dan kebijakan kustom per proyek.
  - Form pembuatan kebijakan dengan kontrol mode (*Suggest only, Automatic with threshold, Require approval*).
  - Ekspor log keputusan AI dalam format CSV dan JSON.

---

### 4.11 User Access Management (`pages/Users.tsx` - Baru di PR #10)
* **Tujuan Pengguna:** Administrator dapat mendaftarkan akun operator baru, menetapkan peran (*Viewer, Operator, Approver, Admin*), dan menerbitkan token otentikasi sekali pakai.
* **Fitur Utama:**
  - Panel pembuatan akun dengan validasi karakter nama yang ketat (regex `[A-Za-z0-9][A-Za-z0-9_.@\-]*`).
  - Penjelasan peran yang jelas di bawah dropdown.
  - Kotak tampilan token dengan tombol *"Copy token"* sekali klik dan pengingat bahwa token tidak dapat dilihat kembali setelah keluar dari halaman.
* **Evaluasi UX:**
  - Alur pembuatan token bersih dan aman. Fokus keyboard otomatis berpindah ke kotak teks token setelah dibuat.

---

### 4.12 Authentication Gateway (`pages/Login.tsx`)
* **Tujuan Pengguna:** Pintu masuk otentikasi menggunakan bearer token API.
* **Fitur Utama:**
  - Kolom token dengan tombol *Show / Hide Password* (ikon mata).
  - Kompatibel dengan password manager browser (input atribut `current-password` dan username terselubung).
  - Pesan penolakan sesi spesifik (misal: token kedaluwarsa atau IP terkena *rate-limit lockout* setelah beberapa kali percobaan gagal).
  - Tautan langsung tanpa token jika sistem berjalan dalam mode `auth_disabled`.

---

## 5. AUDIT AKSESIBILITAS (WCAG 2.2 AA)

| Kriteria WCAG | Status | Evaluasi & Bukti Teknis |
|---|---|---|
| **1.4.3 Contrast (Minimum)** | **LULUS (100%)** | Kontras teks pada Dark Theme (Foreground `#F8FAFC` di atas Card `#1B2336` rasio > 12:1; Muted Foreground `#94A3B8` di atas Card rasio > 5.2:1). Pada Light Theme, Foreground `#1E293B` di atas Card `#FFFFFF` rasio > 14:1. Seluruh status warna (Success, Warning, Danger, Info) memenuhi rasio >= 4.5:1 terhadap latar belakang kartu. |
| **1.4.11 Non-text Contrast** | **LULUS** | Garis batas form input, indikator fasa, dan cincin fokus (*focus ring*) memiliki rasio kontras >= 3:1 terhadap permukaan sekitar. |
| **1.4.10 Reflow** | **LULUS DENGAN CATATAN** | Halaman tidak mengalami horizontal document scroll pada lebar 320 CSS px. Namun ditemukan kompresi kolom ekstrem pada komponen `EvidenceCard` dan `AdvisorPolicies` yang memerlukan perbaikan flex-wrapping (Temuan UI-04 dan UI-05). |
| **2.1.1 Keyboard** | **LULUS DENGAN CATATAN** | Seluruh tombol, modal, menu drawer, dan selektor dapat dioperasikan penuh menggunakan keyboard (Tab, Shift+Tab, Enter, Space, Escape). Celah kecil ada pada scroller kontainer data tabel yang memerlukan `tabIndex={0}` (Temuan UI-02). |
| **2.4.7 Focus Visible** | **LULUS (Sangat Baik)** | Focus ring putih bersih pada dark theme dan biru tegas pada light theme tampil konsisten saat navigasi keyboard. |
| **2.5.5 / 2.5.8 Target Size** | **LULUS (100%)** | Seluruh tombol aksi utama (*Primary buttons*) memiliki ukuran minimum 44x44px. Tombol sekunder memiliki padding target sentuh memadai. |
| **4.1.3 Status Messages** | **LULUS (Sangat Baik)** | Perubahan fasa migrasi, status filter pencarian, dan hasil validasi rencana diumumkan secara tepat melalui container `aria-live="polite"` tanpa menyela aktivitas pengguna. |

---

## 6. EVALUASI ERGONOMI OPERASIONAL & SAFETY GATES

Sebagai sistem orkestrasi pemindahan beban kerja server produksi, keselamatan tindakan (*preventing catastrophic operational errors*) adalah pilar UI/UX terpenting:

1. **Maker-Checker Principle (Pemisahan Tugas):**
   - Operator yang mengajukan permintaan cutover (*cutover request*) tidak dapat menyetujui permintaannya sendiri jika rencana mengaktifkan opsi `Distinct approver`.
   - UI secara transparan menampilkan status: *"Waiting for distinct approver"* sehingga alur kerja audit patuh regulasi tata kelola IT perbankan / enterprise.

2. **Irreversible Action Gates:**
   - **Finalize Migration (Penghapusan VM Sumber di Cloud Lama):**
     Tindakan ini permanen. UI menuntut operator untuk mengetikkan nama mesin virtual secara presisi ke dalam kotak input dialog sebelum tombol merah *"Finalize"* dapat ditekan.
   - **Cancel Plan:**
     Menuntut pengetikan ulang nama rencana migrasi secara lengkap.

3. **Status Clarity (Tidak Ada Status Sukses Semu):**
   - Rencana migrasi yang selesai namun semua VM-nya mengalami *rollback* diberi label abu-abu netral dengan penjelasan tegas: *"Nothing was migrated: every migration rolled back"*. Sistem menolak menampilkan status hijau *"Completed"*, menjaga operator dari kesalahan persepsi ketersediaan layanan.

---

## 7. REKOMENDASI ROADMAP REMEDIASI UNTUK TIM ENGINEERING (TRACK C)

Bagi tim frontend developer yang akan melakukan pembaruan kode di masa mendatang, berikut urutan perbaikan yang disarankan:

### Tahap 1: Resiliensi & Keamanan Tampilan (Quick Wins)
1. Pasang `<ErrorBoundary>` di pembungkus route `dashboard/src/App.tsx` (UI-01).
2. Tambahkan komponen pembungkus `ScrollRegion` pada tabel jaringan di `NetworkPanel.tsx` dan JSON viewer di `RecommendationDetail.tsx` (UI-02).
3. Sesuaikan flex direction menjadi `flex-col sm:flex-row` pada `EvidenceCard.tsx` dan `AdvisorPolicies.tsx` agar tampilan pada 320px tidak tertekan (UI-04, UI-05).

### Tahap 2: Pengalaman Navigasi & Form
4. Tambahkan logika penyesuaian posisi atas/bawah (*auto-flip*) pada tooltip `Term.tsx` (UI-03).
5. Tautkan filter pencarian pada `pages/Plans.tsx` ke dalam URL query parameters (UI-06).
6. Tambahkan pengecekan form kotor (*dirty form confirmation*) saat tombol Escape ditekan pada `PlanCreateDialog.tsx` (UI-07).

### Tahap 3: Pemolesan & Fitur Pengguna
7. Tambahkan indikator status ringkas identitas pengguna dan mode Demo di bar atas saat tampilan mobile/rail aktif (UI-08).
8. Tambahkan tombol pencabutan token pengguna pada antarmuka `pages/Users.tsx` (UI-09).
9. Tambahkan navigasi tautan jangkar (*anchor shortcut bar*) di bagian atas halaman detail migrasi untuk kenyamanan navigasi operator mobile (UI-12).

---

## 8. KESIMPULAN AUDITOR & TANDA TANGAN

Secara arsitektural dan fungsional, antarmuka web **Seamless Migrate** dirancang dengan standar kualitas rekayasa perangkat lunak yang sangat tinggi. Perhatian terhadap detail aksesibilitas, konsistensi token desain, kejelasan pesan error, dan perlindungan terhadap kegagalan operasional cloud berada di atas rata-rata perangkat lunak sejenis.

Dengan menerapkan rekomendasi perbaikan terstruktur di atas (khususnya penambahan error boundary pada lazy routing dan fleksibilitas kartu pada layar sempit), dashboard ini akan mencapai kesempurnaan operasional penuh baik di desktop workstation ruang operasi (*NOC*) maupun pada tablet/smartphone teknisi lapangan.

---

**Auditor & Contributor:**  
**Agentic Gemini**  
GitHub: [@gemini-code-assist](https://github.com/gemini-code-assist) / [@google-gemini](https://github.com/google-gemini)  
Author: `Gemini <gemini-code-assist@users.noreply.github.com>`  
Repository: `imransetiadi/seamless-vms-migration`  
