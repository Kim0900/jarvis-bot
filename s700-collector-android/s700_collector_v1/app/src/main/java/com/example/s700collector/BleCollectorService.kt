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

    override fun onCreate() {
        super.onCreate()
        val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        mgr.createNotificationChannel(NotificationChannel(CHANNEL_ID, "S700 Collector", NotificationManager.IMPORTANCE_LOW))
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
        append("""{"type":"notify_chunk","received_at":"${now()}","hex":"${bytes.hex()}"}""")
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

    private fun saveFrame(frame: ByteArray) {
        val start = if (frame.firstOrNull()?.toInt() == 0x02) 1 else 0
        val end = if (frame.lastOrNull()?.toInt() == 0x03) frame.size - 1 else frame.size
        val ascii = frame.copyOfRange(start, end).toString(Charsets.US_ASCII).replace("\"", "\\\"")
        val meterTime = Regex("""^(\d{12})""").find(ascii)?.groupValues?.get(1)
        append("""{"type":"frame","received_at":"${now()}","meter_time_raw":${meterTime?.let { "\"$it\"" } ?: "null"},"ascii":"$ascii","hex":"${frame.hex()}"}""")
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
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED)
            adapter.bluetoothLeScanner?.stopScan(scanCallback)
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED)
            gatt?.disconnect()
        gatt?.close(); gatt = null; scanning = false
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
