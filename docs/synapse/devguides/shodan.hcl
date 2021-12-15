job "synapse-shodan" {
  datacenters = ["us-east-1a"]

  constraint {
    attribute = "${attr.unique.platform.aws.instance-id}"
    operator = "="
    value = "i-00d8bff789a736b75"
  }

  group "service" {
    task "shodan" {
      driver = "docker"
      config {
        image = "vertexproject/synapse-shodan:v2.0.0"
        auth = [
          {
            username = "username"
            password = "secret"
          }
        ]
        entrypoint = ["sh"]
        command = "-c"
        args = ["cp local/cell.yaml /vertex/storage/ && exec python3 -O -m synapse.servers.cell synmods.shodan.service.Shodan /vertex/storage"]
        volumes = [
          "/data/vertex/shodan/:/vertex/storage",
        ]
        port_map {
          telepath = 27492
        }
        force_pull = true
      }

      env {
        SYN_LOG_LEVEL = "DEBUG"
      }

      service {
        name = "synapse-shodan"
        port = "telepath"
        tags = [
          "telepath"
        ]
      }

      # Example of loading in a cell.yaml file. This is copied to ./local,
      # and in the entrypoint, we copy this file over to the /vertex/storage location.
      # Once that is complete, we launch the service.
      template {
        destination = "local/cell.yaml"
        data = <<EOH
---
auth:passwd: secret
api:key: anotherSecret
...
EOH
      }

      resources {
        cpu    = 1024
        memory = 1024

        network {
          mbits = 10

          port "telepath" {}
        }
      }
    }
  }
}
