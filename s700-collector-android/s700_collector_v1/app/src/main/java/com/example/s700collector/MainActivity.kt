package com.example.s700collector

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {
    private lateinit var status: TextView
    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
            status.text = if (result.values.all { it }) "권한 허용 완료" else "블루투스/알림 권한이 필요합니다."
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
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
}
