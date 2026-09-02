import Foundation
import Testing
@testable import TalkToHermes

struct APIClientTests {
    @Test func buildsAuthenticatedHTTPSStatusRequest() throws {
        let endpoint = try EndpointConfiguration(host: "bridge.example.com", port: 9_443)
        let client = APIClient(endpoint: endpoint, tokenProvider: { "private-token" })

        let request = try client.makeRequest(path: "/v1/status", method: "GET")

        #expect(request.url?.absoluteString == "https://bridge.example.com:9443/v1/status")
        #expect(request.httpMethod == "GET")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer private-token")
        #expect(request.timeoutInterval == 30)
    }

    @Test func acceptsOnlySameOriginRedirects() throws {
        let origin = try #require(URL(string: "https://bridge.example.com:8443"))
        let sameOrigin = try #require(URL(string: "https://bridge.example.com:8443/v1/status"))
        let otherHost = try #require(URL(string: "https://attacker.example:8443/v1/status"))
        let otherPort = try #require(URL(string: "https://bridge.example.com:9443/v1/status"))
        let insecure = try #require(URL(string: "http://bridge.example.com:8443/v1/status"))

        #expect(SameOriginRedirectDelegate.isSameOrigin(sameOrigin, as: origin))
        #expect(!SameOriginRedirectDelegate.isSameOrigin(otherHost, as: origin))
        #expect(!SameOriginRedirectDelegate.isSameOrigin(otherPort, as: origin))
        #expect(!SameOriginRedirectDelegate.isSameOrigin(insecure, as: origin))
    }

    @Test func buildsBoundedMultipartVoiceTurnRequest() throws {
        let endpoint = try EndpointConfiguration(host: "bridge.example.com", port: 8_443)
        let client = APIClient(endpoint: endpoint, tokenProvider: { "private-token" })
        let audio = Data("RIFF-test-wave".utf8)

        let request = try client.makeVoiceTurnRequest(
            conversationID: "conversation-1",
            clientTurnID: "A0B1C2D3-E4F5-4678-9ABC-DEF012345678",
            audio: audio,
            language: "en-US",
            responseStyle: .detailed
        )

        #expect(request.url?.absoluteString == "https://bridge.example.com:8443/v1/conversations/conversation-1/turns")
        #expect(request.httpMethod == "POST")
        #expect(request.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("multipart/form-data; boundary=") == true)
        let body = try #require(request.httpBody)
        let text = try #require(String(data: body, encoding: .utf8))
        #expect(text.contains("name=\"client_turn_id\"\r\n\r\na0b1c2d3-e4f5-4678-9abc-def012345678"))
        #expect(text.contains("name=\"language\"\r\n\r\nen-US"))
        #expect(text.contains("name=\"voice_id\"\r\n\r\ndefault"))
        #expect(text.contains("name=\"include_text\"\r\n\r\ntrue"))
        #expect(text.contains("name=\"response_style\"\r\n\r\ndetailed"))
        #expect(text.contains("filename=\"recording.wav\""))
        #expect(text.contains("RIFF-test-wave"))
    }

    @Test(arguments: [SpeechLanguage.german, .english])
    func transmitsSelectedVoiceTurnSpeechLanguage(_ speechLanguage: SpeechLanguage) throws {
        let endpoint = try EndpointConfiguration(host: "bridge.example.com", port: 8_443)
        let client = APIClient(endpoint: endpoint, tokenProvider: { "private-token" })

        let request = try client.makeVoiceTurnRequest(
            conversationID: "conversation-1",
            clientTurnID: "A0B1C2D3-E4F5-4678-9ABC-DEF012345678",
            audio: Data("RIFF-test-wave".utf8),
            language: speechLanguage.rawValue
        )

        let body = try #require(request.httpBody)
        let text = try #require(String(data: body, encoding: .utf8))
        #expect(text.contains("name=\"language\"\r\n\r\n\(speechLanguage.rawValue)"))
    }

    @Test func defaultsVoiceTurnSpeechLanguageToGerman() throws {
        let endpoint = try EndpointConfiguration(host: "bridge.example.com", port: 8_443)
        let client = APIClient(endpoint: endpoint, tokenProvider: { "private-token" })

        let request = try client.makeVoiceTurnRequest(
            conversationID: "conversation-1",
            clientTurnID: "A0B1C2D3-E4F5-4678-9ABC-DEF012345678",
            audio: Data("RIFF-test-wave".utf8)
        )

        let body = try #require(request.httpBody)
        let text = try #require(String(data: body, encoding: .utf8))
        #expect(text.contains("name=\"language\"\r\n\r\nde"))
    }

    @Test func rejectsOversizedVoiceUploadBeforeNetworking() throws {
        let endpoint = try EndpointConfiguration(host: "bridge.example.com", port: 8_443)
        let client = APIClient(endpoint: endpoint, tokenProvider: { "private-token" })
        let oversized = Data(repeating: 0, count: APIClient.maxAudioBytes + 1)

        #expect(throws: APIClientError.audioTooLarge) {
            try client.makeVoiceTurnRequest(
                conversationID: "conversation-1",
                clientTurnID: "a0b1c2d3-e4f5-4678-9abc-def012345678",
                audio: oversized
            )
        }
    }

    @Test func rejectsNonUUIDClientTurnIdentifier() throws {
        let endpoint = try EndpointConfiguration(host: "bridge.example.com", port: 8_443)
        let client = APIClient(endpoint: endpoint, tokenProvider: { "private-token" })

        #expect(throws: APIClientError.invalidClientTurnID) {
            try client.makeVoiceTurnRequest(
                conversationID: "conversation-1",
                clientTurnID: "client-1",
                audio: Data("RIFF".utf8)
            )
        }
    }
}
