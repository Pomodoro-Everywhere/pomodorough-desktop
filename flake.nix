{
  description = "Pomodorough Linux desktop, command-line, and terminal clients";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.python3.pkgs.buildPythonApplication {
            pname = "pomodorough-linux";
            version = "0.1.2";
            pyproject = true;
            src = pkgs.lib.fileset.toSource {
              root = ./.;
              fileset = pkgs.lib.fileset.unions [
                ./LICENSE
                ./README.md
                ./pyproject.toml
                ./src
                ./deploy
              ];
            };

            build-system = [ pkgs.python3.pkgs.setuptools ];
            dependencies = [ pkgs.python3.pkgs.pyside6 ];
            pythonRemoveDeps = [ "PySide6-Essentials" ];
            nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
            buildInputs = [ pkgs.qt6.qtwayland ];

            postInstall = ''
              install -Dm644 src/pomodorough/resources/icon.svg \
                $out/share/icons/hicolor/scalable/apps/me.egigoka.Pomodorough.svg
              install -Dm644 deploy/me.egigoka.Pomodorough.desktop \
                $out/share/applications/me.egigoka.Pomodorough.desktop
              substituteInPlace $out/share/applications/me.egigoka.Pomodorough.desktop \
                --replace-fail "/usr/bin/env -u LOCALE_ARCHIVE_2_27 @EXEC@" "$out/bin/pomodorough" \
                --replace-fail "@ICON@" "me.egigoka.Pomodorough"
              install -Dm644 deploy/flatpak/me.egigoka.Pomodorough.metainfo.xml \
                $out/share/metainfo/me.egigoka.Pomodorough.metainfo.xml
            '';

            pythonImportsCheck = [ "pomodorough" ];

            meta = {
              description = "KDE-first, local-first Pomodoro timer";
              homepage = "https://github.com/egigoka/pomodorough-linux";
              license = pkgs.lib.licenses.gpl3Plus;
              mainProgram = "pomodorough";
              platforms = pkgs.lib.platforms.linux;
            };
          };
        });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/pomodorough";
          meta.description = "Run the Pomodorough desktop timer";
        };
      });
    };
}
