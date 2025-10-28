{
  description = "Finance tracking web application";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        pythonEnv = pkgs.python311.withPackages (ps: with ps; [
          flask
          sqlalchemy
          pandas
        ]);
        financeTracking = pkgs.writeShellApplication {
          name = "finance-tracking";
          runtimeInputs = [ pythonEnv ];
          text = ''
            exec ${pythonEnv}/bin/python ${./transactions_web_app.py} "$@"
          '';
        };
      in {
        packages = {
          default = financeTracking;
          finance-tracking = financeTracking;
        };

        apps = {
          default = flake-utils.lib.mkApp { drv = financeTracking; };
          finance-tracking = flake-utils.lib.mkApp { drv = financeTracking; };
        };

        devShells.default = pkgs.mkShell {
          packages = [ pythonEnv ];
        };
      }
    ) // {
      overlays.default = final: prev: {
        finance-tracking = self.packages.${final.stdenv.hostPlatform.system}.finance-tracking;
      };

      nixosModules.finance-tracking = { config, lib, pkgs, ... }:
        let
          cfg = config.services.financeTracking;
          types = lib.types;
          defaultPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.finance-tracking;
        in {
          options.services.financeTracking = with types; {
            enable = lib.mkEnableOption "Finance tracking web application";

            package = lib.mkOption {
              type = package;
              default = defaultPackage;
              defaultText = lib.literalExpression "self.packages.${pkgs.stdenv.hostPlatform.system}.finance-tracking";
              description = "Package that provides the finance tracking command.";
            };

            user = lib.mkOption {
              type = str;
              default = "finance-tracking";
              description = "System user that will run the finance tracking service.";
            };

            group = lib.mkOption {
              type = str;
              default = "finance-tracking";
              description = "System group that will run the finance tracking service.";
            };

            manageUser = lib.mkOption {
              type = bool;
              default = true;
              description = "Whether to create and manage the service user automatically.";
            };

            manageGroup = lib.mkOption {
              type = bool;
              default = true;
              description = "Whether to create and manage the service group automatically.";
            };

            dataDir = lib.mkOption {
              type = str;
              default = "/var/lib/finance-tracking";
              description = "Directory that will contain the sqlite database and state.";
            };

            databaseUrl = lib.mkOption {
              type = nullOr str;
              default = null;
              description = "Custom SQLAlchemy database URL. Defaults to a sqlite file inside dataDir when null.";
            };

            host = lib.mkOption {
              type = str;
              default = "127.0.0.1";
              description = "Host interface the development server should bind to.";
            };

            port = lib.mkOption {
              type = ints.unsigned;
              default = 5000;
              description = "Port the development server listens on.";
            };

            environment = lib.mkOption {
              type = attrsOf str;
              default = {};
              description = "Extra environment variables for the finance tracking process.";
            };

            serviceConfig = lib.mkOption {
              type = attrs;
              default = {};
              description = "Extra systemd.service options merged into the finance tracking service.";
            };

            openFirewall = lib.mkOption {
              type = bool;
              default = false;
              description = "Open the firewall for the configured port.";
            };

            timer = {
              enable = lib.mkOption {
                type = bool;
                default = false;
                description = "Enable a systemd timer that applies active subscriptions.";
              };

              description = lib.mkOption {
                type = str;
                default = "Apply finance tracking subscriptions";
                description = "Description for the systemd timer.";
              };

              month = lib.mkOption {
                type = nullOr str;
                default = null;
                description = "Optional YYYY-MM override passed to the apply-subscriptions command.";
              };

              timerConfig = lib.mkOption {
                type = attrsOf (types.oneOf [ str bool int ]);
                default = {
                  OnCalendar = "daily";
                  Persistent = true;
                };
                description = "systemd timer configuration attributes.";
              };

              environment = lib.mkOption {
                type = attrsOf str;
                default = {};
                description = "Additional environment variables for the apply timer.";
              };

              serviceConfig = lib.mkOption {
                type = attrs;
                default = {};
                description = "Extra systemd.service options for the apply timer.";
              };

              wantedBy = lib.mkOption {
                type = listOf str;
                default = [ "timers.target" ];
                description = "Targets that the timer should be wanted by.";
              };
            };
          };

          config = lib.mkIf cfg.enable (
            let
              dbUrl =
                if cfg.databaseUrl != null
                then cfg.databaseUrl
                else "sqlite:///" + cfg.dataDir + "/transactions.db";
              serviceEnv = cfg.environment // { DATABASE_URL = dbUrl; };
              startScript = pkgs.writeShellScript "finance-tracking-start" ''
                exec ${cfg.package}/bin/finance-tracking \
                  --database ${lib.escapeShellArg dbUrl} \
                  --host ${lib.escapeShellArg cfg.host} \
                  --port ${lib.escapeShellArg (toString cfg.port)}
              '';
              applyScript = pkgs.writeShellScript "finance-tracking-apply" ''
                exec ${cfg.package}/bin/finance-tracking \
                  --apply-subscriptions \
                  --database ${lib.escapeShellArg dbUrl} \
                  ${lib.optionalString (cfg.timer.month != null) "--month ${lib.escapeShellArg cfg.timer.month}"}
              '';
              timerEnv = serviceEnv // cfg.timer.environment;
            in
              (
                {
                  assertions = [
                    {
                      assertion = cfg.port <= 65535;
                      message = "services.financeTracking.port must be within the TCP port range.";
                    }
                  ];

                  users.groups = lib.mkIf cfg.manageGroup { ${cfg.group} = {}; };

                  users.users = lib.mkIf cfg.manageUser {
                    ${cfg.user} = {
                      isSystemUser = true;
                      group = cfg.group;
                      home = cfg.dataDir;
                      createHome = true;
                    };
                  };

                  systemd.tmpfiles.rules = [
                    "d ${cfg.dataDir} 0750 ${cfg.user} ${cfg.group} -"
                  ];

                  systemd.services.finance-tracking = {
                    description = "Finance tracking web service";
                    after = [ "network.target" ];
                    wantedBy = [ "multi-user.target" ];
                    serviceConfig = {
                      ExecStart = startScript;
                      WorkingDirectory = cfg.dataDir;
                      User = cfg.user;
                      Group = cfg.group;
                      Restart = "on-failure";
                      RestartSec = 5;
                    } // cfg.serviceConfig;
                    environment = serviceEnv;
                  };

                  systemd.services."finance-tracking-apply" = lib.mkIf cfg.timer.enable {
                    description = "Apply finance tracking subscriptions";
                    serviceConfig = {
                      Type = "oneshot";
                      ExecStart = applyScript;
                      WorkingDirectory = cfg.dataDir;
                      User = cfg.user;
                      Group = cfg.group;
                    } // cfg.timer.serviceConfig;
                    environment = timerEnv;
                  };

                  systemd.timers."finance-tracking-apply" = lib.mkIf cfg.timer.enable {
                    description = cfg.timer.description;
                    wantedBy = cfg.timer.wantedBy;
                    timerConfig = cfg.timer.timerConfig;
                  };
                }
                // lib.mkIf cfg.openFirewall {
                  networking.firewall.allowedTCPPorts = [ cfg.port ];
                }
              )
          );
        };
    };
}
