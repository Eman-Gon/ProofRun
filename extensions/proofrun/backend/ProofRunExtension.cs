using Duplo.Ai.Studio.Extensibility;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.DependencyInjection;

namespace Duplo.Extension.ProofRun;

public class ProofRunExtension : IDuploExtension
{
    public void Configure(WebApplicationBuilder builder)
        => builder.Services.AddHostedService<ProofRunWorker>();
}
