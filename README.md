# VoxelLive Android — Alpha scaffold

Wrapper awal Android untuk dashboard dan backend Python VoxelLive 2.0.1.

## Build APK via GitHub Actions
Buka tab **Actions** → **Build VoxelLive APK** → **Run workflow**. Setelah selesai, unduh artifact `VoxelLive-Android-debug`.

## Integrasi awal
- WebView membuka dashboard lokal `http://127.0.0.1:8766/`.
- Foreground service menjalankan Python lewat Chaquopy.
- Minecraft Bedrock WebSocket tetap di port 6464.
- Data pengguna diarahkan ke direktori privat aplikasi.

## Status penting
Ini adalah kerangka integrasi alpha, belum APK rilis yang teruji. Build harus membuktikan TikTokLive 7.0.1 dan dependensinya dapat dipasang di runtime Android Chaquopy. Setelah build, masih wajib uji pada perangkat: TikTok LIVE, Minecraft LAN/WebSocket, koneksi saat aplikasi di latar belakang, cloud login, preset, dan penyimpanan data. Build APK berhasil bukan bukti semua fitur berjalan. Jangan gunakan sebagai rilis publik sebelum pengujian tersebut lulus.
