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
    poetry
    python311Full
    gcc
    gnumake
  ] ++ pkgs.lib.optional (builtins.getEnv "IN_NIX_SHELL" == "pure") [ docker-client ];
}

