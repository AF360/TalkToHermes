import SwiftUI

struct ContentView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @StateObject private var model = VoiceViewModel()
    @State private var showsSettings = false

    var body: some View {
        NavigationStack {
            GeometryReader { geometry in
                let metrics = VoiceLayout.metrics(for: geometry.size.width)
                ZStack {
                    HermesBackground()
                    if metrics.mode == .wide {
                        wideContent(metrics)
                    } else {
                        compactContent(metrics)
                    }
                }
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    Group {
                        if metrics.mode == .wide {
                            wideBottomBar(metrics)
                        } else {
                            primaryControls(diameter: metrics.recordButtonDiameter)
                                .frame(maxWidth: .infinity)
                                .padding(.top, 12)
                                .padding(.bottom, 8)
                        }
                    }
                    .background(.ultraThinMaterial)
                    .overlay(alignment: .top) {
                        Divider().opacity(0.45)
                    }
                }
                .toolbarBackground(.hidden, for: .navigationBar)
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button {
                            model.newConversation()
                        } label: {
                            Image(systemName: "square.and.pencil")
                        }
                        .accessibilityLabel("Neue Unterhaltung")
                        .disabled(locksNavigation)
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            model.prepareForSettings()
                            showsSettings = true
                        } label: {
                            Image(systemName: "gearshape.fill")
                        }
                        .accessibilityLabel("Einstellungen")
                        .disabled(locksNavigation)
                    }
                }
                .sheet(isPresented: $showsSettings, onDismiss: {
                    Task { await model.refreshConfiguration() }
                }) {
                    SettingsView()
                }
                .task {
                    await model.refreshConfiguration()
                }
                .alert(
                    "TalkToHermes-Fehler",
                    isPresented: Binding(
                        get: { model.errorMessage != nil },
                        set: { if !$0 { model.errorMessage = nil } }
                    )
                ) {
                    Button("OK", role: .cancel) { model.errorMessage = nil }
                } message: {
                    Text(model.errorMessage ?? String(localized: "Unbekannter Fehler"))
                }
                .animation(reduceMotion ? nil : .snappy, value: model.chatHistory)
                .animation(reduceMotion ? nil : .snappy, value: model.approvalRequired)
            }
        }
        .tint(.hermesCopper)
    }

    private func compactContent(_ metrics: VoiceLayoutMetrics) -> some View {
        VStack(spacing: 0) {
            if VoiceLayout.pinsBrandHeader(for: metrics.mode) {
                brandHeader
                    .frame(maxWidth: 680)
                    .padding(.horizontal, 16)
                    .padding(.top, 4)
                    .padding(.bottom, 8)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 16) {
                        voiceStage(diameter: metrics.orbDiameter)
                        conversationContent
                        chatBottomAnchor
                    }
                    .frame(maxWidth: 680)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 24)
                    .frame(maxWidth: .infinity)
                }
                .scrollIndicators(.hidden)
                .onChange(of: model.chatHistory.exchanges.last?.id) { _, latestID in
                    scrollToLatest(latestID, using: proxy)
                }
                .onChange(of: model.approvalRequired) { previous, current in
                    if ChatAutoScroll.shouldRevealApproval(from: previous, to: current) {
                        scrollToBottom(using: proxy)
                    }
                }
            }
        }
    }

    private func wideContent(_ metrics: VoiceLayoutMetrics) -> some View {
        VStack(spacing: 18) {
            brandHeader

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 0) {
                        conversationContent
                            .frame(maxWidth: .infinity)
                        chatBottomAnchor
                    }
                    .padding(.bottom, 24)
                }
                .scrollIndicators(.hidden)
                .onChange(of: model.chatHistory.exchanges.last?.id) { _, latestID in
                    scrollToLatest(latestID, using: proxy)
                }
                .onChange(of: model.approvalRequired) { previous, current in
                    if ChatAutoScroll.shouldRevealApproval(from: previous, to: current) {
                        scrollToBottom(using: proxy)
                    }
                }
            }
        }
        .padding(.horizontal, 28)
        .padding(.top, 12)
    }

    private func wideBottomBar(_ metrics: VoiceLayoutMetrics) -> some View {
        Group {
            if VoiceLayout.bottomBarMode(
                isAccessibilitySize: dynamicTypeSize.isAccessibilitySize
            ) == .stacked {
                VStack(spacing: 16) {
                    wideVoiceStatus(metrics)
                    primaryControls(diameter: metrics.recordButtonDiameter)
                        .frame(maxWidth: .infinity)
                }
            } else {
                HStack(spacing: 28) {
                    wideVoiceStatus(metrics)
                    primaryControls(diameter: metrics.recordButtonDiameter)
                        .frame(width: 220)
                }
            }
        }
        .padding(.horizontal, 28)
        .padding(.top, 10)
        .padding(.bottom, 8)
    }

    private func wideVoiceStatus(_ metrics: VoiceLayoutMetrics) -> some View {
        HStack(spacing: 16) {
            HermesVoiceOrb(
                isRecording: model.recorder.isRecording,
                isBusy: model.isBusy || model.isStartingRecording || model.isRefreshingConfiguration,
                isPlaying: model.isPlaying,
                level: model.recorder.level,
                diameter: metrics.orbDiameter
            )

            VStack(alignment: .leading, spacing: 4) {
                Text(stageTitle)
                    .font(.headline)
                Text(stageSubtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if model.recorder.isRecording {
                    Text(durationText)
                        .font(.system(.caption, design: .monospaced, weight: .semibold))
                        .foregroundStyle(.red)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private var conversationContent: some View {
        VStack(spacing: 18) {
            if model.chatHistory.exchanges.isEmpty {
                if !model.approvalRequired {
                    introPanel
                }
            } else {
                ChatTimeline(
                    exchanges: model.chatHistory.exchanges,
                    hasPlayableResponse: model.hasPlayableResponse,
                    isPlaying: model.isPlaying,
                    onTogglePlayback: model.togglePlayback,
                    onStopPlayback: model.stopResponsePlayback
                )
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }

            if model.degradedAudio {
                fallbackNotice
            }

            if model.approvalRequired {
                approvalPanel
                    .transition(.scale.combined(with: .opacity))
            }
        }
    }

    private var chatBottomAnchor: some View {
        Color.clear
            .frame(height: 1)
            .id("chat-bottom")
            .accessibilityHidden(true)
    }

    private func scrollToLatest(_ latestID: String?, using proxy: ScrollViewProxy) {
        guard latestID != nil else { return }
        scrollToBottom(using: proxy)
    }

    private func scrollToBottom(using proxy: ScrollViewProxy) {
        withAnimation(reduceMotion ? nil : .snappy) {
            proxy.scrollTo("chat-bottom", anchor: .bottom)
        }
    }

    private var locksNavigation: Bool {
        model.isBusy || model.isStartingRecording || model.isRefreshingConfiguration ||
        model.canCancel || model.recorder.isRecording
    }

    private var brandHeader: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center) {
                brandName
                Spacer(minLength: 12)
                HermesStatusPill(isReady: model.isReady, text: model.statusText)
            }
            VStack(alignment: .leading, spacing: 10) {
                brandName
                HermesStatusPill(isReady: model.isReady, text: model.statusText)
            }
        }
    }

    private var brandName: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("TalkToHermes")
                .font(.title2.weight(.bold))
                .tracking(-0.4)
            Text(String(format: String(localized: "Mit %@ sprechen"), model.assistantName))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func voiceStage(diameter: CGFloat) -> some View {
        VStack(spacing: 2) {
            HermesVoiceOrb(
                isRecording: model.recorder.isRecording,
                isBusy: model.isBusy || model.isStartingRecording || model.isRefreshingConfiguration,
                isPlaying: model.isPlaying,
                level: model.recorder.level,
                diameter: diameter
            )

            Text(stageTitle)
                .font(.title2.weight(.semibold))
                .multilineTextAlignment(.center)
                .contentTransition(.numericText())

            Text(stageSubtitle)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)

            if model.recorder.isRecording {
                Text(durationText)
                    .font(.system(.body, design: .monospaced, weight: .semibold))
                    .foregroundStyle(.red)
                    .padding(.top, 8)
                    .accessibilityLabel(
                        String(format: String(localized: "Aufnahmedauer %@"), durationText)
                    )
            }
        }
    }

    private var stageTitle: String {
        if model.recorder.isRecording { return String(localized: "Ich höre zu") }
        if model.approvalRequired { return String(localized: "Deine Entscheidung") }
        if model.isPlaying {
            return String(format: String(localized: "%@ spricht"), model.assistantName)
        }
        if model.isBusy || model.isStartingRecording { return String(localized: "Einen Moment …") }
        if !model.responseText.isEmpty {
            return String(format: String(localized: "Antwort von %@"), model.assistantName)
        }
        return model.isReady
            ? String(localized: "Was möchtest du wissen?")
            : String(localized: "Sicher verbinden")
    }

    private var stageSubtitle: String {
        if model.recorder.isRecording { return String(localized: "Tippe erneut, wenn du fertig bist.") }
        if model.isReady { return model.statusText }
        return String(localized: "Öffne die Einstellungen, um die verschlüsselte Verbindung einzurichten.")
    }

    private var durationText: String {
        let seconds = Int(model.recorder.elapsedTime)
        return String(format: "%d:%02d", seconds / 60, seconds % 60)
    }

    private var introPanel: some View {
        HermesPanel {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "waveform.and.mic")
                    .font(.title2)
                    .foregroundStyle(Color.hermesCopper)
                    .frame(width: 32)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Natürlich sprechen")
                        .font(.headline)
                    Text(
                        String(
                            format: String(localized: "Knopf nicht gedrückt halten: einmal zum Starten tippen, einmal zum Senden. Danach antwortet %@ automatisch per Sprache."),
                            model.assistantName
                        )
                    )
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .allowsTightening(true)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private var fallbackNotice: some View {
        Label("Diese Antwort verwendet die lokale Fallback-Stimme.", systemImage: "exclamationmark.triangle.fill")
            .font(.footnote.weight(.medium))
            .foregroundStyle(.orange)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 4)
    }

    private var approvalPanel: some View {
        HermesPanel {
            VStack(alignment: .leading, spacing: 15) {
                Label("Freigabe erforderlich", systemImage: "checkmark.shield.fill")
                    .font(.headline)
                    .foregroundStyle(Color.hermesCopper)
                Text(
                    String(
                        format: String(localized: "%@ möchte eine Aktion ausführen. Die Freigabe gilt nur für diesen Vorgang."),
                        model.assistantName
                    )
                )
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                ViewThatFits(in: .horizontal) {
                    HStack {
                        approvalButtons
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        approvalButtons
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var approvalButtons: some View {
        Button("Einmal erlauben") { model.approveOnce() }
            .buttonStyle(.borderedProminent)
        Button("Ablehnen", role: .destructive) { model.denyApproval() }
            .buttonStyle(.bordered)
    }

    private var primaryActionLabel: String {
        if model.recorder.isRecording { return String(localized: "Zum Senden tippen") }
        if model.isPlaying { return String(localized: "Unterbrechen und sprechen") }
        return String(localized: "Sprechen")
    }

    private var primaryActionHint: String {
        if model.recorder.isRecording {
            return String(
                format: String(localized: "Beendet die Aufnahme und sendet sie an %@."),
                model.assistantName
            )
        }
        if model.isPlaying {
            return String(
                format: String(localized: "Stoppt %@ und startet sofort eine neue Sprachaufnahme."),
                model.assistantName
            )
        }
        return String(localized: "Startet eine neue Sprachaufnahme.")
    }

    private func primaryControls(diameter: CGFloat) -> some View {
        VStack(spacing: 14) {
            Button {
                model.toggleRecording()
            } label: {
                ZStack {
                    Circle()
                        .fill(model.recorder.isRecording ? Color.red : Color.hermesCopper)
                        .frame(width: diameter, height: diameter)
                        .shadow(
                            color: (model.recorder.isRecording ? Color.red : Color.hermesCopper).opacity(0.32),
                            radius: 18,
                            y: 8
                        )
                    if model.isStartingRecording {
                        ProgressView()
                            .tint(.white)
                    } else {
                        Image(systemName: model.recorder.isRecording ? "stop.fill" : "mic.fill")
                            .font(.system(size: diameter * 0.34, weight: .semibold))
                            .foregroundStyle(.white)
                    }
                }
            }
            .buttonStyle(.plain)
            .disabled(!model.canSpeak && !model.recorder.isRecording)
            .accessibilityLabel(primaryActionLabel)
            .accessibilityHint(primaryActionHint)

            Text(primaryActionLabel)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(model.canSpeak || model.recorder.isRecording ? .primary : .secondary)

            if model.canCancel {
                Button(role: .destructive) {
                    model.cancelTurn()
                } label: {
                    Label("Vorgang abbrechen", systemImage: "xmark.circle")
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(.top, 2)
    }
}

#if DEBUG
#Preview("iPhone", traits: .fixedLayout(width: 390, height: 844)) {
    ContentView()
}

#Preview("iPad", traits: .fixedLayout(width: 1024, height: 1366)) {
    ContentView()
}
#endif
