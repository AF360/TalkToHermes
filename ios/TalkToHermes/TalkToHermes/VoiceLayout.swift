import CoreGraphics

nonisolated enum VoiceLayoutMode: Equatable, Sendable {
    case compact
    case wide
}

nonisolated enum VoiceBottomBarMode: Equatable, Sendable {
    case horizontal
    case stacked
}

nonisolated enum ChatAutoScroll {
    static func shouldRevealApproval(from previous: Bool, to current: Bool) -> Bool {
        !previous && current
    }
}

nonisolated struct VoiceLayoutMetrics: Equatable, Sendable {
    let mode: VoiceLayoutMode
    let orbDiameter: CGFloat
    let recordButtonDiameter: CGFloat
}

nonisolated enum VoiceLayout {
    static func bottomBarMode(isAccessibilitySize: Bool) -> VoiceBottomBarMode {
        isAccessibilitySize ? .stacked : .horizontal
    }

    static func metrics(for width: CGFloat) -> VoiceLayoutMetrics {
        if width >= 760 {
            return VoiceLayoutMetrics(
                mode: .wide,
                orbDiameter: 76,
                recordButtonDiameter: 88
            )
        }
        return VoiceLayoutMetrics(
            mode: .compact,
            orbDiameter: 104,
            recordButtonDiameter: 72
        )
    }
}
