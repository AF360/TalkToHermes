import SwiftUI

struct ChatTimeline: View {
    let exchanges: [ChatExchange]
    let hasPlayableResponse: Bool
    let isPlaying: Bool
    let onTogglePlayback: () -> Void
    let onStopPlayback: () -> Void

    var body: some View {
        LazyVStack(spacing: 18) {
            ForEach(exchanges) { exchange in
                if let userText = exchange.userText {
                    userBubble(userText, id: exchange.id)
                }
                assistantEntry(exchange)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func userBubble(_ userText: String, id: String) -> some View {
        HStack(alignment: .top) {
            Spacer(minLength: 44)
            Text(userText)
                .font(.body)
                .textSelection(.enabled)
                .foregroundStyle(Color.hermesGraphite)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(
                    Color.hermesCopper,
                    in: RoundedRectangle(cornerRadius: 19, style: .continuous)
                )
                .accessibilityIdentifier("ChatUserBubble-\(id)")
        }
    }

    private func assistantEntry(_ exchange: ChatExchange) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            if !exchange.tools.isEmpty {
                HStack(spacing: 7) {
                    Image(systemName: "hammer.fill")
                        .foregroundStyle(Color.hermesAmber)
                    Text(exchange.tools.map(ChatToolName.display).joined(separator: " · "))
                }
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.leading, 48)
                .accessibilityLabel(
                    String(
                        format: String(localized: "Verwendete Tools: %@"),
                        exchange.tools.map(ChatToolName.display).joined(separator: ", ")
                    )
                )
                .accessibilityIdentifier("ChatTools-\(exchange.id)")
            }

            HStack(alignment: .top, spacing: 10) {
                assistantAvatar(for: exchange.assistantName)
                VStack(alignment: .leading, spacing: 9) {
                    Text(exchange.assistantName)
                        .font(.caption.weight(.bold))
                        .foregroundStyle(Color.hermesCopper)
                    Text(exchange.assistantText)
                        .font(.body)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)

                    if exchange.id == exchanges.last?.id {
                        latestActions(for: exchange)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 13)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    .regularMaterial,
                    in: RoundedRectangle(cornerRadius: 21, style: .continuous)
                )
                .overlay {
                    RoundedRectangle(cornerRadius: 21, style: .continuous)
                        .stroke(.primary.opacity(0.08), lineWidth: 1)
                }
                .accessibilityIdentifier("ChatAssistantBubble-\(exchange.id)")
            }
        }
    }

    private func assistantAvatar(for assistantName: String) -> some View {
        Circle()
            .fill(
                LinearGradient(
                    colors: [.hermesCream, .hermesCopper],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
            )
            .frame(width: 38, height: 38)
            .overlay {
                Text(String(assistantName.prefix(1)).uppercased())
                    .font(.headline.weight(.bold))
                    .foregroundStyle(Color.hermesGraphite)
            }
            .accessibilityHidden(true)
    }

    @ViewBuilder
    private func latestActions(for exchange: ChatExchange) -> some View {
        HStack(spacing: 12) {
            if hasPlayableResponse {
                Button {
                    onTogglePlayback()
                } label: {
                    Label(
                        isPlaying ? String(localized: "Pause") : String(localized: "Erneut abspielen"),
                        systemImage: isPlaying ? "pause.fill" : "play.fill"
                    )
                }
                .buttonStyle(.bordered)

                if isPlaying {
                    Button(role: .destructive) {
                        onStopPlayback()
                    } label: {
                        Label("Wiedergabe stoppen", systemImage: "stop.fill")
                    }
                    .buttonStyle(.bordered)
                }
            }

            ShareLink(item: exchange.assistantText) {
                Label("Antwort teilen", systemImage: "square.and.arrow.up")
            }
            .buttonStyle(.bordered)
        }
        .labelStyle(.iconOnly)
    }
}

#if DEBUG
private struct ChatTimelinePreviewContent: View {
    var body: some View {
        ZStack {
            HermesBackground()
            ScrollView {
                ChatTimeline(
                    exchanges: [
                        ChatExchange(
                            id: "preview-1",
                            userText: "Öffne bitte den Code für TalkToHermes.",
                            assistantText: "Die aktuelle Arbeitskopie ist geöffnet und der Build ist sauber.",
                            assistantName: "Johanna",
                            tools: ["OpenCodeTool"]
                        ),
                        ChatExchange(
                            id: "preview-2",
                            userText: "Und jetzt Super Asteroids.",
                            assistantText: "Der Wechsel ist etwas sprunghaft, aber Super Asteroids ist gestartet.",
                            assistantName: "Johanna",
                            tools: ["SuperAsteroidsTool"]
                        )
                    ],
                    hasPlayableResponse: true,
                    isPlaying: false,
                    onTogglePlayback: {},
                    onStopPlayback: {}
                )
                .padding()
            }
        }
    }
}

#Preview("Chat iPhone", traits: .fixedLayout(width: 390, height: 844)) {
    ChatTimelinePreviewContent()
}

#Preview("Chat iPad", traits: .fixedLayout(width: 1024, height: 1366)) {
    ChatTimelinePreviewContent()
}
#endif
