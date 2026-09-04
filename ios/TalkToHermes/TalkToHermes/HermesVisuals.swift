import SwiftUI

extension Color {
    static let hermesGraphite = Color(red: 0.075, green: 0.078, blue: 0.09)
    static let hermesCopper = Color(red: 0.88, green: 0.46, blue: 0.24)
    static let hermesAmber = Color(red: 0.97, green: 0.66, blue: 0.41)
    static let hermesCream = Color(red: 1.0, green: 0.85, blue: 0.69)
}

struct HermesBackground: View {
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        ZStack {
            (colorScheme == .dark ? Color.hermesGraphite : Color(.systemGroupedBackground))
            RadialGradient(
                colors: [Color.hermesCopper.opacity(colorScheme == .dark ? 0.20 : 0.12), .clear],
                center: .top,
                startRadius: 20,
                endRadius: 430
            )
        }
        .ignoresSafeArea()
    }
}

struct HermesStatusPill: View {
    let isReady: Bool
    let text: String

    var body: some View {
        HStack(spacing: 7) {
            Circle()
                .fill(isReady ? Color.green : Color.orange)
                .frame(width: 8, height: 8)
                .shadow(color: (isReady ? Color.green : Color.orange).opacity(0.55), radius: 5)
            Text(text)
                .font(.caption.weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("ConnectionStatus")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().stroke(.primary.opacity(0.08), lineWidth: 1))
    }
}

struct HermesVoiceOrb: View {
    let isRecording: Bool
    let isBusy: Bool
    let isPlaying: Bool
    let level: Double
    let diameter: CGFloat

    init(
        isRecording: Bool,
        isBusy: Bool,
        isPlaying: Bool,
        level: Double,
        diameter: CGFloat = 236
    ) {
        self.isRecording = isRecording
        self.isBusy = isBusy
        self.isPlaying = isPlaying
        self.level = level
        self.diameter = diameter
    }

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pulses = false

    private var isActive: Bool { isRecording || isBusy || isPlaying }
    private var accent: Color { isRecording ? .red : .hermesCopper }

    var body: some View {
        ZStack {
            Circle()
                .fill(accent.opacity(0.10))
                .frame(width: diameter, height: diameter)
                .scaleEffect(pulses && isActive ? 1.08 : 0.92)
            Circle()
                .fill(accent.opacity(0.14))
                .frame(width: diameter * 0.82, height: diameter * 0.82)
                .scaleEffect(pulses && isActive ? 0.96 : 1.04)
            Circle()
                .fill(
                    LinearGradient(
                        colors: [.hermesCream, .hermesCopper],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
                .frame(width: diameter * 0.63, height: diameter * 0.63)
                .shadow(color: accent.opacity(0.34), radius: 28, y: 12)
                .overlay {
                    Circle()
                        .stroke(.white.opacity(0.42), lineWidth: 1)
                }

            HStack(alignment: .center, spacing: 8) {
                ForEach(Array([0.42, 0.72, 1.0, 0.72, 0.42].enumerated()), id: \.offset) { index, factor in
                    Capsule()
                        .fill(Color.hermesGraphite.opacity(0.94))
                        .frame(
                            width: max(5, diameter * 0.034),
                            height: diameter * 0.23 * factor * (isRecording ? max(level, 0.35) : 1)
                        )
                        .animation(
                            reduceMotion ? nil : .spring(response: 0.18, dampingFraction: 0.62).delay(Double(index) * 0.015),
                            value: level
                        )
                }
            }
        }
        .frame(height: diameter + 14)
        .accessibilityHidden(true)
        .onAppear { updatePulse() }
        .onChange(of: isActive) { _, _ in updatePulse() }
        .onChange(of: reduceMotion) { _, _ in updatePulse() }
    }

    private func updatePulse() {
        guard !reduceMotion, isActive else {
            pulses = false
            return
        }
        withAnimation(.easeInOut(duration: 1.25).repeatForever(autoreverses: true)) {
            pulses = true
        }
    }
}

struct HermesPanel<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        content
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .stroke(.primary.opacity(0.08), lineWidth: 1)
            }
    }
}
