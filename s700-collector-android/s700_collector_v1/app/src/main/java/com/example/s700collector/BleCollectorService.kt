package com.example.s700collector

import android.Manifest
import android.app.*
import android.bluetooth.*
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Intent
import android.content.pm.PackageManager
import android.os.IBinder
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import java.io.File
import java.text.SimpleDateFormat
import java.util.*

class BleCollectorService : Service() {
    companion object {
        const val ACTION_START = "com.example.s700collector.START"
        const val ACTION_STOP = "com.example.s700collector.STOP"
        private const val CHANNEL_ID = "s700_collector"
        private val SERVICE_UUID = UUID.fromString("0000a002-0000-1000-8000-00805f9b34fb")
        private val NOTIFY_UUID = UUID.fromString("0000c301-0000-1000-8000-00805f9b34fb")
        private val CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }

    private val bluetoothManager by lazy { getSystemService(BLUETOOTH_SERVICE) as BluetoothManager }
    private val adapter get() = bluetoothManager.adapter
    private var gatt: BluetoothGatt? = null
    private var scanning = false
    private val frameBuffer = mutableListOf<Byte>()
    private var inFrame = false
    // CASPER 조사(2026-08-29): onCharacteristicChanged가 레거시(2-param)/신규
    // (3-param, ByteArray) 두 오버로드로 구현돼 있는데, 일부 기기/OS 조합에서
    // 동일 notify 이벤트에 대해 시스템이 둘 다 호출하는 사례가 보고되어 있음.
    // frameBuffer가 동기화 없는 단일 공유 버퍼라, 두 콜백이 겹쳐 실행되면
    // 서로 다른 청크의 바이트가 뒤섞여 저장되는 것을 실제 로그(§5 손상사례)
    // 재현분석으로 확인 — bufferLock으로 handleChunk 실행을 직렬화.
    private val bufferLock = Any()
    // 완전동일 프레임 반복(§7 중복notify)에 대한 dedup — raw notify_chunk
    // 로깅(append 첫줄)은 dedup 없이 원본 그대로 전부 보존, "frame"(파싱결과)
    // 레코드에만 적용.
    private var lastSavedFrameHex: String? = null

    // CASPER v2(2026-08-29): Health Monitor — payload length histogram.
    // "98자가 S700 전체 protocol의 고정길이"라는 전제가 실측(44/54자)과
    // 안맞았던 사고 재발방지용 핵심 진단자료(§17). 실제 payload 길이
    // 분포가 쌓여야 Decoder/checksum 구조를 안전하게 확정할 수 있음.
    private val payloadLengthHistogram = mutableMapOf<Int, Int>()
    private var notifyCount = 0
    private var packetCount = 0
    private var longBusinessFrameCount = 0
    private var shortFrameCount = 0
    private var checksumPassCount = 0
    private var checksumFailCount = 0
    private val LONG_BUSINESS_FRAME_LENGTH = 98  // 현재까지 관측된 후보값, 확정 아님(v2 §0 정정)

    private val healthHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val healthRunnable = object : Runnable {
        override fun run() {
            logHealthSnapshot()
            healthHandler.postDelayed(this, 180_000L)  // 3분마다
        }
    }

    override fun onCreate() {
        super.onCreate()
        val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        mgr.createNotificationChannel(NotificationChannel(CHANNEL_ID, "S700 Collector", NotificationManager.IMPORTANCE_LOW))
        healthHandler.postDelayed(healthRunnable, 180_000L)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            cleanup(); stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(); return START_NOT_STICKY
        }
        startForeground(1, notification("S700 탐색 중"))
        startScan()
        return START_STICKY
    }

    private fun startScan() {
        if (scanning) return
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED) return
        adapter.bluetoothLeScanner?.let {
            scanning = true
            it.startScan(scanCallback)
            update("S700 탐색 중")
        }
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            if (ActivityCompat.checkSelfPermission(this@BleCollectorService, Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) return
            val name = result.device.name ?: result.scanRecord?.deviceName
            if (name?.startsWith("GIT_") == true) {
                if (ActivityCompat.checkSelfPermission(this@BleCollectorService, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED)
                    adapter.bluetoothLeScanner?.stopScan(this)
                scanning = false
                gatt = result.device.connectGatt(this@BleCollectorService, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
                update("$name 연결 중")
            }
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                update("S700 연결됨")
                if (ActivityCompat.checkSelfPermission(this@BleCollectorService, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED)
                    g.requestMtu(247)
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                g.close()
                if (gatt == g) gatt = null
                update("연결 끊김 · 재탐색")
                startScan()
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            meta("mtu", mtu.toString())
            if (ActivityCompat.checkSelfPermission(this@BleCollectorService, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED)
                g.discoverServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val ch = g.getService(SERVICE_UUID)?.getCharacteristic(NOTIFY_UUID) ?: run {
                meta("error", "A002/C301 not found"); g.disconnect(); return
            }
            if (ActivityCompat.checkSelfPermission(this@BleCollectorService, Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) return
            g.setCharacteristicNotification(ch, true)
            val cccd = ch.getDescriptor(CCCD_UUID)
            if (cccd != null) {
                cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                g.writeDescriptor(cccd)
                update("C301 Notify 수신 대기")
            }
        }

        @Deprecated("Compatibility callback")
        override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            handleChunk(characteristic.value ?: byteArrayOf())
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, value: ByteArray) {
            handleChunk(value)
        }
    }

    private fun handleChunk(bytes: ByteArray) {
        // raw notify_chunk 로깅은 원본 그대로, dedup/락 없이 전부 보존(절대원칙 §14)
        notifyCount++
        append("""{"type":"notify_chunk","received_at":"${now()}","hex":"${bytes.hex()}"}""")
        synchronized(bufferLock) {
            for (b in bytes) {
                val u = b.toInt() and 0xff
                if (u == 0x02) { frameBuffer.clear(); frameBuffer.add(b); inFrame = true }
                else if (inFrame) frameBuffer.add(b)
                if (inFrame && u == 0x03) {
                    val frame = ByteArray(frameBuffer.size) { frameBuffer[it] }
                    saveFrame(frame)
                    frameBuffer.clear()
                    inFrame = false
                }
            }
        }
    }

    private fun saveFrame(frame: ByteArray) {
        packetCount++
        val hexFull = frame.hex()
        if (hexFull == lastSavedFrameHex) return  // 완전동일 프레임(§7 중복notify) 저장 스킵
        lastSavedFrameHex = hexFull
        val start = if (frame.firstOrNull()?.toInt() == 0x02) 1 else 0
        val end = if (frame.lastOrNull()?.toInt() == 0x03) frame.size - 1 else frame.size
        val ascii = frame.copyOfRange(start, end).toString(Charsets.US_ASCII).replace("\"", "\\\"")
        val meterTime = Regex("""^(\d{12})""").find(ascii)?.groupValues?.get(1)

        // CASPER v2(2026-08-29) Protocol Classifier — §3. "98자 = 전체 protocol
        // 고정길이"라는 이전 전제가 실측(44/54자)과 안맞았던 것이 확인되어,
        // 이제는 길이로 "분류"만 하고(폐기하지 않음), LONG_BUSINESS_FRAME으로
        // 분류된 것에 한해서만 checksum을 "시도"하되 Trip 생성 등 확정처리는
        // 하지 않음(다음 실기기 로그로 PASS율 실측 후 Decoder/Trip 단계 진행).
        val payloadLength = ascii.length
        val frameClass = if (payloadLength == LONG_BUSINESS_FRAME_LENGTH) "LONG_BUSINESS_FRAME" else "SHORT_FRAME"
        payloadLengthHistogram[payloadLength] = (payloadLengthHistogram[payloadLength] ?: 0) + 1
        if (frameClass == "LONG_BUSINESS_FRAME") longBusinessFrameCount++ else shortFrameCount++

        var checksumReceived: String? = null
        var checksumCalculated: String? = null
        var checksumValid: Boolean? = null
        // CASPER Decoder(2026-08-30, 마기승인) — 실측검증(원본로그 실제분석,
        // checksum 98.4% PASS, 필드매핑 22건중21건 영수증정확일치) 통과한
        // 필드만 파싱. Trip Finalizer(요금확정 판단)는 마기지시대로 보류 —
        // 여기서는 구조화된 값을 "보여주기"만 하고 raw_calls 등 확정저장은
        // 하지 않음(로컬 JSONL 파싱결과 레코드로만 남김).
        var meterFare: Int? = null
        var tripStart: String? = null
        var tripCloseFlag: String? = null
        var primaryState: String? = null
        var secondaryState: String? = null
        var decodeWarning: String? = null

        if (frameClass == "LONG_BUSINESS_FRAME") {
            try {
                val sum = ascii.substring(0, 96).toByteArray(Charsets.US_ASCII).sumOf { it.toInt() and 0xFF }
                val calculated = (-sum) and 0xFF
                val received = ascii.substring(96, 98).toInt(16)
                checksumCalculated = "%02X".format(calculated)
                checksumReceived = "%02X".format(received)
                checksumValid = calculated == received
                if (checksumValid == true) checksumPassCount++ else checksumFailCount++

                // checksum PASS인 것만 디코딩 시도(손상프레임 억지해석 방지)
                if (checksumValid == true) {
                    primaryState = ascii.substring(18, 20)
                    secondaryState = ascii.substring(20, 22)
                    meterFare = ascii.substring(22, 28).toIntOrNull()
                    tripStart = ascii.substring(44, 56)
                    tripCloseFlag = ascii.substring(82, 84)

                    // 안전장치: checksum PASS해도 필드값 자체가 이상한 사례
                    // (실측 3,003원 케이스: trip_start='002260082820', state=07/00)
                    // 발견됨 — 관측된 정상 state 4종 외 값이면 경고 플래그.
                    val knownStates = setOf("02/02", "0A/08", "04/04", "07/00")
                    val stateKey = "$primaryState/$secondaryState"
                    if (stateKey !in knownStates) {
                        decodeWarning = "unknown_state($stateKey)"
                    } else if (!Regex("""^\d{12}$""").matches(tripStart ?: "")) {
                        decodeWarning = "invalid_trip_start_format"
                    }
                }
            } catch (e: Exception) {
                meta("checksum_calc_error", e.message ?: "unknown")
            }
        }

        append(
            """{"type":"frame","received_at":"${now()}","meter_time_raw":${meterTime?.let { "\"$it\"" } ?: "null"},""" +
            """"ascii":"$ascii","hex":"$hexFull","payload_length":$payloadLength,"frame_class":"$frameClass",""" +
            """"checksum_received":${checksumReceived?.let { "\"$it\"" } ?: "null"},""" +
            """"checksum_calculated":${checksumCalculated?.let { "\"$it\"" } ?: "null"},""" +
            """"checksum_valid":${checksumValid ?: "null"},""" +
            """"meter_fare":${meterFare ?: "null"},""" +
            """"trip_start":${tripStart?.let { "\"$it\"" } ?: "null"},""" +
            """"trip_close_flag":${tripCloseFlag?.let { "\"$it\"" } ?: "null"},""" +
            """"primary_state":${primaryState?.let { "\"$it\"" } ?: "null"},""" +
            """"secondary_state":${secondaryState?.let { "\"$it\"" } ?: "null"},""" +
            """"decode_warning":${decodeWarning?.let { "\"$it\"" } ?: "null"},""" +
            """"decoder_version":"s700-decoder-v0.3"}"""
        )
    }

    // CASPER v2(2026-08-29) Health Monitor §17 — payload length histogram을
    // 주기적으로 meta 레코드로 남김(진단용, 3분마다).
    private fun logHealthSnapshot() {
        val histJson = payloadLengthHistogram.entries.joinToString(",") { "\"${it.key}\":${it.value}" }
        append(
            """{"type":"health","received_at":"${now()}","notify_count":$notifyCount,"packet_count":$packetCount,""" +
            """"long_business_frame_count":$longBusinessFrameCount,"short_frame_count":$shortFrameCount,""" +
            """"checksum_pass_count":$checksumPassCount,"checksum_fail_count":$checksumFailCount,""" +
            """"payload_length_histogram":{$histJson}}"""
        )
    }

    private fun append(line: String) {
        val dir = File(getExternalFilesDir(null), "S700").apply { mkdirs() }
        val file = File(dir, "s700_${SimpleDateFormat("yyyyMMdd", Locale.KOREA).format(Date())}.jsonl")
        file.appendText(line + "\n")
    }

    private fun meta(k: String, v: String) = append("""{"type":"meta","received_at":"${now()}","$k":"$v"}""")
    private fun now() = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.KOREA).format(Date())
    private fun ByteArray.hex() = joinToString("") { "%02X".format(it) }

    private fun notification(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("S700 Collector")
            .setContentText(text)
            .setOngoing(true)
            .build()

    private fun update(text: String) {
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(1, notification(text))
    }

    private fun cleanup() {
        healthHandler.removeCallbacks(healthRunnable)
        logHealthSnapshot()
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED)
            adapter.bluetoothLeScanner?.stopScan(scanCallback)
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED)
            gatt?.disconnect()
        gatt?.close(); gatt = null; scanning = false
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
