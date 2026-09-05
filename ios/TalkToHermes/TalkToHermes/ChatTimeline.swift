import SwiftUI

struct ChatTimeline: View {
    @Environment(\.hermesPalette) private var palette
    @Environment(\.colorScheme) private var colorScheme
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
                .foregroundStyle(palette.foregroundOnAccent)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(
                    palette.messageBackground,
                    in: RoundedRectangle(cornerRadius: 19, style: .continuous)
                )
                .accessibilityIdentifier("ChatUserBubble-\(id)")
        }
    }

    private func assistantEntry(_ exchange: ChatExchange) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top, spacing: 10) {
                assistantAvatar(for: exchange.assistantName)
                VStack(alignment: .leading, spacing: 9) {
                    Text(exchange.assistantName)
                        .font(.caption.weight(.bold))
                        .foregroundStyle(palette.normalForeground(for: colorScheme))
                    if !exchange.toolInvocations.isEmpty {
                        toolInvocationBadges(
                            exchange.toolInvocations,
                            exchangeID: exchange.id
                        )
                    }
                    Text(exchange.assistantText)
                        .font(.body)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("ChatAssistantBubble-\(exchange.id)")

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
            }
        }
    }

    private func toolInvocationBadges(
        _ invocations: [ChatToolInvocation],
        exchangeID: String
    ) -> some View {
        LazyVGrid(
            columns: [
                GridItem(
                    .adaptive(
                        minimum: ToolActivityLayout.markerTouchDiameter,
                        maximum: ToolActivityLayout.markerTouchDiameter
                    ),
                    spacing: 7
                )
            ],
            alignment: .leading,
            spacing: 7
        ) {
            ForEach(invocations.indices, id: \.self) { index in
                ToolInvocationBadge(
                    invocation: invocations[index],
                    ordinal: index + 1,
                    accessibilityID: invocations[index].accessibilityIdentifier(
                        exchangeID: exchangeID
                    )
                )
            }
        }
    }

    private func assistantAvatar(for assistantName: String) -> some View {
        Circle()
            .fill(
                LinearGradient(
                    colors: [palette.highlight, palette.accent],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
            )
            .frame(width: 38, height: 38)
            .overlay {
                Text(String(assistantName.prefix(1)).uppercased())
                    .font(.headline.weight(.bold))
                    .foregroundStyle(palette.background)
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

private struct ToolInvocationBadge: View {
    @Environment(\.hermesPalette) private var palette
    @Environment(\.colorScheme) private var colorScheme
    let invocation: ChatToolInvocation
    let ordinal: Int
    let accessibilityID: String
    @State private var showsDetails = false

    var body: some View {
        Button {
            showsDetails = true
        } label: {
            ZStack(alignment: .topTrailing) {
                Circle()
                    .fill(.ultraThinMaterial)
                    .frame(width: 34, height: 34)
                    .overlay {
                        Circle().stroke(badgeColor.opacity(0.75), lineWidth: 1)
                    }
                Image(systemName: toolIcon)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(badgeColor)
                    .frame(width: 34, height: 34)
                Text("\(ordinal)")
                    .font(.system(size: 8, weight: .bold, design: .rounded))
                    .foregroundStyle(palette.background)
                    .frame(minWidth: 13, minHeight: 13)
                    .background(palette.highlight, in: Circle())
                    .offset(x: 4, y: -4)
            }
            .frame(
                width: ToolActivityLayout.markerTouchDiameter,
                height: ToolActivityLayout.markerTouchDiameter
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel(
            String(
                format: String(localized: "Details zu %@, Aufruf %d"),
                ChatToolName.display(invocation.name),
                ordinal
            )
        )
        .accessibilityHint("Zeigt sichere Details zu diesem Tool-Aufruf.")
        .accessibilityIdentifier(accessibilityID)
        .popover(isPresented: $showsDetails, arrowEdge: .top) {
            ToolInvocationPopover(invocation: invocation, ordinal: ordinal)
                .presentationCompactAdaptation(.popover)
        }
    }

    private var badgeColor: Color { palette.controlAccent(for: colorScheme) }

    private var toolIcon: String {
        let lowered = invocation.name.lowercased()
        if lowered.contains("browser") || lowered.contains("web") { return "globe" }
        if lowered.contains("terminal") { return "terminal.fill" }
        if lowered.contains("code") { return "chevron.left.forwardslash.chevron.right" }
        if lowered.contains("asteroid") || lowered.contains("game") { return "gamecontroller.fill" }
        return "hammer.fill"
    }
}

private struct ToolInvocationPopover: View {
    @Environment(\.hermesPalette) private var palette
    @Environment(\.colorScheme) private var colorScheme
    let invocation: ChatToolInvocation
    let ordinal: Int
    @Environment(\.locale) private var locale

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 10) {
                    Image(systemName: "hammer.fill")
                        .foregroundStyle(palette.controlAccent(for: colorScheme))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(ChatToolName.display(invocation.name))
                            .font(.headline)
                        Text(String(format: String(localized: "Tool-Aufruf %d"), ordinal))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Divider()

                Text(invocation.summary ?? String(localized: "Keine weiteren Details verfügbar."))
                    .font(.subheadline)
                    .fixedSize(horizontal: false, vertical: true)

                VStack(spacing: 8) {
                    detailRow("Status", String(localized: "Aufgerufen"))
                    detailRow("Gestartet", startedText)
                    detailRow("Freigabe", approvalText)
                    if let riskText {
                        detailRow("Risiko", riskText)
                    }
                }
            }
            .padding(18)
        }
        .frame(
            minWidth: 280,
            idealWidth: 320,
            maxWidth: 340,
            maxHeight: 560,
            alignment: .leading
        )
    }

    private func detailRow(_ label: LocalizedStringKey, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.caption)
                .multilineTextAlignment(.trailing)
        }
    }


    private var startedText: String {
        guard let raw = invocation.startedAt else { return "—" }
        let precise = ISO8601DateFormatter()
        precise.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let basic = ISO8601DateFormatter()
        basic.formatOptions = [.withInternetDateTime]
        guard let date = precise.date(from: raw) ?? basic.date(from: raw) else { return "—" }
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.dateStyle = .none
        formatter.timeStyle = .medium
        return formatter.string(from: date)
    }

    private var approvalText: String {
        switch invocation.approvalRequired {
        case true: String(localized: "Erforderlich")
        case false: String(localized: "Nicht erforderlich")
        case nil: String(localized: "Nicht verfügbar")
        }
    }

    private var riskText: String? {
        switch invocation.risk {
        case "low": String(localized: "Niedrig")
        case "medium": String(localized: "Mittel")
        case "high": String(localized: "Hoch")
        case .some: String(localized: "Nicht verfügbar")
        case nil: nil
        }
    }
}

#if DEBUG
private struct ChatTimelinePreviewContent: View {
    private let toolInvocations = [
        ChatToolInvocation(
            id: "tool-6",
            name: "read_file",
            summary: "Datei gelesen",
            status: "invoked",
            startedAt: "2026-09-04T17:00:00Z",
            approvalRequired: false,
            risk: nil
        ),
        ChatToolInvocation(
            id: "tool-7",
            name: "mcp__home_assistant__ha_get_state",
            summary: "Status in Home Assistant abgerufen",
            status: "invoked",
            startedAt: "2026-09-04T17:00:01Z",
            approvalRequired: true,
            risk: "medium"
        ),
    ]

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
                            toolInvocations: toolInvocations
                        ),
                        ChatExchange(
                            id: "preview-2",
                            userText: "Wie ist morgen das Wetter in Bochum?",
                            assistantText: "Ich habe die Wetterinformationen abgerufen.",
                            assistantName: "Johanna",
                            tools: ["web_search"]
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

#Preview("Tool-Details", traits: .sizeThatFitsLayout) {
    ToolInvocationPopover(
        invocation: ChatToolInvocation(
            id: "tool-7",
            name: "mcp__home_assistant__ha_get_state",
            summary: "Status in Home Assistant abgerufen",
            status: "invoked",
            startedAt: "2026-09-04T17:00:01Z",
            approvalRequired: true,
            risk: "medium"
        ),
        ordinal: 2
    )
}
#endif
