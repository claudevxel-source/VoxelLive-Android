package com.voxellive.android;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.File;
public class VoxelLiveService extends Service {
    private static final String CHANNEL = "voxellive_runtime";
    @Override public void onCreate() {
        super.onCreate();
        if (Build.VERSION.SDK_INT >= 26) getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(CHANNEL, "VoxelLive runtime", NotificationManager.IMPORTANCE_LOW));
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        startForeground(2001, b.setContentTitle("VoxelLive aktif").setContentText("Menjaga koneksi TikTok LIVE dan Minecraft").setSmallIcon(android.R.drawable.stat_notify_sync_noanim).setOngoing(true).build());
        new Thread(() -> { try {
            if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
            PyObject server = Python.getInstance().getModule("server");
            File data = new File(getFilesDir(), "voxellive-data"); if (!data.exists()) data.mkdirs();
            server.callAttr("start_background", data.getAbsolutePath());
        } catch (Throwable e) { android.util.Log.e("VoxelLive", "Backend gagal dimulai", e); } }, "VoxelLive-Python-Startup").start();
    }
    @Override public int onStartCommand(Intent intent, int flags, int id) { return START_STICKY; }
    @Override public IBinder onBind(Intent intent) { return null; }
}
