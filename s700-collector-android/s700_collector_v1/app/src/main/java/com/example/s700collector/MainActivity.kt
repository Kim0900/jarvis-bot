package com.example.s700collector

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Button
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {
    private lateinit var status: TextView

    // CASPER 대시보드(2026-08-31, 대표님요청 "수집중인지 에러는 없는지 알수가
    // 없다") — Service가 SharedPreferences에 기록한 상태를 2초마다 읽어서 표시.
    private lateinit var dashLastReceived: TextView
    private lateinit var dashCounts: TextView
    private lateinit var dashChecksum: TextView
    private lateinit var dashTrip: TextView
    private lateinit var dashRecentTrips: TextView
    private val dashHandler = Handler(Looper.getMainLooper())
    private val dashRunnable = object : Runnable {
        override fun run() {
            refreshDashboard()
            dashHandler.postDelayed(this, 2000L)
        }
    }

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
            status.text = if (result.values.all { it }) "권한 허용 완료" else "블루투스/알림 권한이 필요합니다."
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
        dashLastReceived = findViewById(R.id.dashLastReceived)
        dashCounts = findViewById(R.id.dashCounts)
        dashChecksum = findViewById(R.id.dashChecksum)
        dashTrip = findViewById(R.id.dashTrip)
        dashRecentTrips = findViewById(R.id.dashRecentTrips)

        permissionLauncher.launch(arrayOf(
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.BLUETOOTH_CONNECT,
            Manifest.permission.POST_NOTIFICATIONS
        ))
        findViewById<Button>(R.id.startButton).setOnClickListener {
            ContextCompat.startForegroundService(this, Intent(this, BleCollectorService::class.java).apply {
                action = BleCollectorService.ACTION_START
            })
            status.text = "수집 시작 요청됨"
        }
        findViewById<Button>(R.id.stopButton).setOnClickListener {
            startService(Intent(this, BleCollectorService::class.java).apply {
                action = BleCollectorService.ACTION_STOP
            })
            status.text = "수집 중지 요청됨"
        }
    }

    override fun onResume() {
        super.onResume()
        dashHandler.post(dashRunnable)
    }

    override fun onPause() {
        super.onPause()
        dashHandler.removeCallbacks(dashRunnable)
    }

    private fun refreshDashboard() {
        val prefs = getSharedPreferences("s700_dashboard", MODE_PRIVATE)
        val statusText = prefs.getString("status_text", "대기 중")
        val lastReceivedAt = prefs.getString("last_received_at", "")
        val updatedAtEpoch = prefs.getLong("updated_at_epoch", 0L)
        val notifyCount = prefs.getInt("notify_count", 0)
        val packetCount = prefs.getInt("packet_count", 0)
        val checksumPass = prefs.getInt("checksum_pass_count", 0)
        val checksumFail = prefs.getInt("checksum_fail_count", 0)
        val tripCount = prefs.getInt("trip_count", 0)
        val tripFareSum = prefs.getInt("trip_fare_sum", 0)
        val tripDistanceKm = prefs.getFloat("trip_distance_km", 0f)
        val recentTrips = prefs.getString("recent_trips", "") ?: ""

        // 마지막 수신 후 5분 넘게 갱신이 없으면 "수신 지연" 경고 — 대표님 요청의
        // 핵심("에러는 없는지 알수가 없다")에 대한 직접 응답.
        val staleWarning = if (updatedAtEpoch > 0 && System.currentTimeMillis() - updatedAtEpoch > 5 * 60_000L) {
            " ⚠️ 5분+ 무응답"
        } else ""

        dashLastReceived.text = "상태: $statusText$staleWarning\n마지막 수신: ${if (lastReceivedAt.isNullOrEmpty()) "-" else lastReceivedAt}"
        dashCounts.text = "수신 ${notifyCount}건 / 프레임 ${packetCount}건"
        val checksumTotal = checksumPass + checksumFail
        val passRate = if (checksumTotal > 0) String.format("%.1f", checksumPass * 100.0 / checksumTotal) else "-"
        dashChecksum.text = "checksum PASS $checksumPass / FAIL $checksumFail (통과율 $passRate%)"
        dashTrip.text = "확정 Trip ${tripCount}건 / ${tripFareSum}원 / ${String.format("%.2f", tripDistanceKm)}km"
        dashRecentTrips.text = if (recentTrips.isEmpty()) "(없음)" else recentTrips.split("|").joinToString("\n")
    }
}
