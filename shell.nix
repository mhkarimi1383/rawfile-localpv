let
  sources = import ./nix/sources.nix;
  pkgs = import sources.nixpkgs {};
in
pkgs.mkShell {
  name = "rawfile-shell";

  buildInputs = with pkgs; [
    kubectl
    kubernetes-helm-wrapped
    helm-docs
    nixos-shell
    kind
    git
    python311
    (poetry.override { python3 = python311; })
    gcc
    gnumake
    btrfs-progs
    stdenv.cc.cc.lib
  ] ++ pkgs.lib.optional (builtins.getEnv "IN_NIX_SHELL" == "pure") [ docker-client ];
  shellHook = ''
    export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib"
    poetry install
    source $(poetry env info -p)/bin/activate
  '';
  postShellHook = ''
    deactivate
  '';
}

