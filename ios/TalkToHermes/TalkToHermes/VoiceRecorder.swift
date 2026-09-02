import AVFoundation
import Combine
import Foundation

nonisolated enum VoiceRecorderError: Error, LocalizedError {
    case microphoneDenied
    case recordingUnavailable

    var errorDescription: String? {
        switch self {
        case .microphoneDenied:
            String(localized: "Der Mikrofonzugriff wurde nicht erlaubt.")
        case .recordingUnavailable:
            String(localized: "Die Sprachaufnahme ist derzeit nicht verfügbar.")
        }
    }
}

@MainActor
final class VoiceRecorder: NSObject, ObservableObject {
    static let maximumDuration: TimeInterval = 60

    @Published private(set) var isRecording = false
    @Published private(set) var level: Double = 0
    @Published private(set) var elapsedTime: TimeInterval = 0

    private var recorder: AVAudioRecorder?
    private var recordingURL: URL?
    private var meteringTask: Task<Void, Never>?
    private let recordingDirectory: URL

    override init() {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        recordingDirectory = base.appending(path: "TalkToHermesRecordings", directoryHint: .isDirectory)
        super.init()
        preparePrivateDirectoryAndRemoveStaleFiles()
    }

    func start() async throws {
        let granted = await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { allowed in
                continuation.resume(returning: allowed)
            }
        }
        guard granted else {
            throw VoiceRecorderError.microphoneDenied
        }
        try Task.checkCancellation()

        let session = AVAudioSession.sharedInstance()
        var createdURL: URL?
        do {
            try session.setCategory(.playAndRecord, mode: .spokenAudio, options: [.defaultToSpeaker, .allowBluetoothHFP])
            try session.setActive(true)

            let url = recordingDirectory.appending(path: "recording-\(UUID().uuidString).wav")
            createdURL = url
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: 16_000,
                AVNumberOfChannelsKey: 1,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsFloatKey: false,
            ]
            let manager = FileManager.default
            guard manager.createFile(
                atPath: url.path,
                contents: Data(),
                attributes: [
                    .posixPermissions: 0o600,
                    .protectionKey: FileProtectionType.completeUnlessOpen,
                ]
            ) else {
                throw VoiceRecorderError.recordingUnavailable
            }
            let newRecorder = try AVAudioRecorder(url: url, settings: settings)
            newRecorder.isMeteringEnabled = true
            guard newRecorder.prepareToRecord() else {
                throw VoiceRecorderError.recordingUnavailable
            }
            try manager.setAttributes(
                [
                    .posixPermissions: 0o600,
                    .protectionKey: FileProtectionType.completeUnlessOpen,
                ],
                ofItemAtPath: url.path
            )
            guard newRecorder.record(forDuration: Self.maximumDuration) else {
                throw VoiceRecorderError.recordingUnavailable
            }
            recorder = newRecorder
            recordingURL = url
            isRecording = true
            startMetering()
        } catch {
            if let createdURL {
                try? FileManager.default.removeItem(at: createdURL)
            }
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            throw error
        }
    }

    func stop() throws -> URL {
        guard let recorder, let recordingURL else {
            throw VoiceRecorderError.recordingUnavailable
        }
        recorder.stop()
        meteringTask?.cancel()
        meteringTask = nil
        self.recorder = nil
        self.recordingURL = nil
        isRecording = false
        level = 0
        elapsedTime = 0
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        return recordingURL
    }

    func discard() {
        meteringTask?.cancel()
        meteringTask = nil
        recorder?.stop()
        recorder = nil
        if let recordingURL {
            try? FileManager.default.removeItem(at: recordingURL)
        }
        recordingURL = nil
        isRecording = false
        level = 0
        elapsedTime = 0
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func startMetering() {
        meteringTask?.cancel()
        elapsedTime = 0
        meteringTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, let recorder = self.recorder, self.isRecording else { return }
                recorder.updateMeters()
                let linearPower = pow(10, Double(recorder.averagePower(forChannel: 0)) / 20)
                self.level = min(max(linearPower * 4.5, 0.04), 1)
                self.elapsedTime = recorder.currentTime
                try? await Task.sleep(for: .milliseconds(80))
            }
        }
    }

    private func preparePrivateDirectoryAndRemoveStaleFiles() {
        let manager = FileManager.default
        try? manager.createDirectory(
            at: recordingDirectory,
            withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUnlessOpen]
        )
        try? manager.setAttributes(
            [
                .posixPermissions: 0o700,
                .protectionKey: FileProtectionType.completeUnlessOpen,
            ],
            ofItemAtPath: recordingDirectory.path
        )
        guard let files = try? manager.contentsOfDirectory(
            at: recordingDirectory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else { return }
        for file in files where file.lastPathComponent.hasPrefix("recording-") {
            try? manager.removeItem(at: file)
        }
    }
}
