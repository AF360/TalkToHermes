import Foundation
import Testing
@testable import TalkToHermes

struct TalkToHermesTests {
    @Test func createsNormalizedConfigurableSecureBaseURL() throws {
        let configuration = try EndpointConfiguration(host: " Bridge.Example.COM. ", port: 9_443)
        #expect(configuration.host == "bridge.example.com")
        #expect(configuration.port == 9_443)
        #expect(try configuration.baseURL.absoluteString == "https://bridge.example.com:9443")
    }

    @Test func rejectsInvalidHosts() {
        for host in ["", "https://bridge.example.com", "bridge.example.com/path", "bridge.example.com:8443", "-bridge.example.com", "bad_label.example.com"] {
            #expect(throws: EndpointConfigurationError.invalidHost) {
                try EndpointConfiguration(host: host, port: 8_443)
            }
        }
    }

    @Test func rejectsPortsOutsideValidRange() {
        for port in [0, 65_536] {
            #expect(throws: EndpointConfigurationError.invalidPort) {
                try EndpointConfiguration(host: "bridge.example.com", port: port)
            }
        }
    }
}
