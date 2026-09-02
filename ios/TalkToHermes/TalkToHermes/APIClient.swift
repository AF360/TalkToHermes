import Foundation

typealias AppTokenProvider = @Sendable () throws -> String

nonisolated enum APIClientError: Error, Equatable, LocalizedError {
    case audioTooLarge
    case answerAudioTooLarge
    case invalidClientTurnID
    case invalidContentType
    case invalidResponse
    case httpStatus(Int)

    var errorDescription: String? {
        switch self {
        case .audioTooLarge:
            String(localized: "Die Aufnahme ist zu groß zum Übertragen.")
        case .answerAudioTooLarge:
            String(localized: "Die Audioantwort ist zu groß zum Abspielen.")
        case .invalidClientTurnID:
            String(localized: "Die Anfrage-ID ist ungültig.")
        case .invalidContentType:
            String(localized: "Die Bridge hat ein unerwartetes Audioformat geliefert.")
        case .invalidResponse:
            String(localized: "Die Bridge hat eine ungültige Antwort geliefert.")
        case let .httpStatus(status):
            String(
                format: String(localized: "Die Bridge hat mit HTTP-Status %lld geantwortet."),
                Int64(status)
            )
        }
    }
}

nonisolated final class SameOriginRedirectDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let origin: URL

    init(origin: URL) {
        self.origin = origin
    }

    static func isSameOrigin(_ candidate: URL, as origin: URL) -> Bool {
        candidate.scheme?.lowercased() == origin.scheme?.lowercased()
            && candidate.host?.lowercased() == origin.host?.lowercased()
            && candidate.port == origin.port
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let destination = request.url,
              Self.isSameOrigin(destination, as: origin) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}

nonisolated struct APIClient: Sendable {
    static let maxAudioBytes = 10 * 1024 * 1024
    static let maxAnswerAudioBytes = 32 * 1024 * 1024

    let endpoint: EndpointConfiguration
    let tokenProvider: AppTokenProvider
    let timeout: TimeInterval
    let session: URLSession

    init(
        endpoint: EndpointConfiguration,
        tokenProvider: @escaping AppTokenProvider,
        timeout: TimeInterval = 30,
        session: URLSession? = nil
    ) {
        self.endpoint = endpoint
        self.tokenProvider = tokenProvider
        self.timeout = timeout
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
            configuration.urlCache = nil
            let origin = try! endpoint.baseURL
            self.session = URLSession(
                configuration: configuration,
                delegate: SameOriginRedirectDelegate(origin: origin),
                delegateQueue: nil
            )
        }
    }

    func makeRequest(path: String, method: String) throws -> URLRequest {
        let baseURL = try endpoint.baseURL
        let normalizedPath = path.hasPrefix("/") ? String(path.dropFirst()) : path
        let url = baseURL.appending(path: normalizedPath)
        var request = URLRequest(
            url: url,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.httpMethod = method
        request.setValue("Bearer \(try tokenProvider())", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    func makeVoiceTurnRequest(
        conversationID: String,
        clientTurnID: String,
        audio: Data,
        language: String = "de",
        responseStyle: VoiceResponseStyle = .short
    ) throws -> URLRequest {
        guard audio.count <= Self.maxAudioBytes else {
            throw APIClientError.audioTooLarge
        }
        guard let parsedTurnID = UUID(uuidString: clientTurnID) else {
            throw APIClientError.invalidClientTurnID
        }
        let canonicalTurnID = parsedTurnID.uuidString.lowercased()
        let boundary = "TalkToHermes-\(UUID().uuidString)"
        var request = try makeRequest(
            path: "/v1/conversations/\(conversationID)/turns",
            method: "POST"
        )
        request.timeoutInterval = 60
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        var body = Data()
        func append(_ value: String) {
            body.append(Data(value.utf8))
        }
        func field(_ name: String, _ value: String) {
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
            append("\(value)\r\n")
        }
        field("client_turn_id", canonicalTurnID)
        field("language", language)
        field("voice_id", "default")
        field("include_text", "true")
        field("response_style", responseStyle.rawValue)
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"audio\"; filename=\"recording.wav\"\r\n")
        append("Content-Type: audio/wav\r\n\r\n")
        body.append(audio)
        append("\r\n--\(boundary)--\r\n")
        request.httpBody = body
        return request
    }

    func status() async throws -> StatusResponse {
        try await send(try makeRequest(path: "/v1/status", method: "GET"), as: StatusResponse.self)
    }

    func createConversation() async throws -> ConversationResponse {
        try await send(
            try makeRequest(path: "/v1/conversations", method: "POST"),
            as: ConversationResponse.self
        )
    }

    func createVoiceTurn(
        conversationID: String,
        clientTurnID: String,
        audio: Data,
        language: String = "de",
        responseStyle: VoiceResponseStyle = .short
    ) async throws -> TurnAcceptedResponse {
        try await send(
            try makeVoiceTurnRequest(
                conversationID: conversationID,
                clientTurnID: clientTurnID,
                audio: audio,
                language: language,
                responseStyle: responseStyle
            ),
            as: TurnAcceptedResponse.self
        )
    }

    func turn(_ turnID: String) async throws -> TurnResponse {
        try await send(
            try makeRequest(path: "/v1/turns/\(turnID)", method: "GET"),
            as: TurnResponse.self
        )
    }

    func audio(_ turnID: String) async throws -> Data {
        let request = try makeRequest(path: "/v1/turns/\(turnID)/audio", method: "GET")
        let (bytes, response) = try await session.bytes(for: request)
        let http = try validatedHTTPResponse(response)
        guard http.mimeType == "audio/wav" || http.mimeType == "audio/x-wav" else {
            throw APIClientError.invalidContentType
        }
        if http.expectedContentLength > Self.maxAnswerAudioBytes {
            throw APIClientError.answerAudioTooLarge
        }
        var data = Data()
        if http.expectedContentLength > 0 {
            data.reserveCapacity(min(Int(http.expectedContentLength), Self.maxAnswerAudioBytes))
        }
        for try await byte in bytes {
            guard data.count < Self.maxAnswerAudioBytes else {
                throw APIClientError.answerAudioTooLarge
            }
            data.append(byte)
        }
        return data
    }

    func approve(_ turnID: String, decision: String) async throws {
        var request = try makeRequest(path: "/v1/turns/\(turnID)/approval", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(ApprovalRequest(decision: decision))
        _ = try await sendData(request)
    }

    func cancel(_ turnID: String) async throws {
        _ = try await sendData(try makeRequest(path: "/v1/turns/\(turnID)/cancel", method: "POST"))
    }

    func deleteConversation(_ conversationID: String) async throws {
        _ = try await sendData(
            try makeRequest(path: "/v1/conversations/\(conversationID)", method: "DELETE")
        )
    }

    private func send<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        let data = try await sendData(request)
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func sendData(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: request)
        _ = try validatedHTTPResponse(response)
        return data
    }

    private func validatedHTTPResponse(_ response: URLResponse) throws -> HTTPURLResponse {
        guard let http = response as? HTTPURLResponse else {
            throw APIClientError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIClientError.httpStatus(http.statusCode)
        }
        return http
    }
}
