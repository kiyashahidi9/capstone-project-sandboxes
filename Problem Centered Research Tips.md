Focus on engineering problems, not features

One of the challenges of researching existing tools, products, and services is distinguishing between the engineering problems that they solve, and the features that they offer.

While the line can be blurry, we want to stay in the realm of articulating the engineering problems, rather than enumerating the features of a given tool.

What is the difference?

Engineering problems tend to express a set of tradeoffs in order to satisfy the constraints of a specific use case. An application is capable of doing X, instead of Y. So for example, selecting a time series database due to the temporal nature of the data, but as a result, limiting the types of queries that can be performed.

Features, on the other hand, tend to be additive. There are no engineering tradeoffs to adding extra features, they expand the use case, rather than targeting a more specific problem. For example, building an SDK that supports JavaScript, Python, and Go is an example of adding features (more supported languages). Adding features tends not to be impressive when talking to other engineers because they rarely represent interesting engineering challenges (usually they just take additional time).

Ignore Marketing / Aspirational Copy
The marketing copy always exaggerates and claims to solve much bigger problems than the solution actually does. We can still use the solution as a hint to the niche problem it's addressing, but you don't typically find this information on their homepage.

Instead, you have to look at users (usually developers) who are implementing this solution. It's the same way you don't gauge the quality of a restaurant by its homepage, but by user reviews. From those reviews, you can typically start to get an idea for exactly what type of restaurant this is (fast food, sit-down, super fancy, etc). If you only look at a restaurant's homepage, it'll say it's "the best food for everyone".

Narrow the Problem
As you're researching existing products or services, always think about the real and likely niche problems the service is addressing. For bigger services and products, you might need to look at a specific feature within the service to get to a problem niche enough for coherent analysis.

Zoom image.png
Grafana's nav menu. Each of these links likely address its own distinct engineering use case

If you're constantly fuzzy about the specific use case that the solution is solving, narrow down the problem. It's ok to try to get very specific when narrowing the problem. Some questions you can ask are:

what actions does a developer take to implement this solution?
what exact steps does this solution eliminate in a developer's workflow? Note: if there's too much magic here, like "auto-whatever", think about what it's automating.
how would a developer solve the problem without this solution? What does that look like if this solution didn't exist?
That line of questioning will usually get you to the problem this solution is solving. If your answer to the above is "well, the developer just uses another tool", then you haven't found the _true_ problem this solution solves.

Tips
google around for users of the solutions you're looking at. In this scenario, "users" actually means "developers". If no developers are using this solution, then it's not the right solution to look at (probably) because the problem we're interested in discovering should be a technical one (as opposed to a business workflow one).
look at Wikipedia articles about the solution, if available. Wikipedia tends to cut straight to the tradeoffs and explicitly state where this solution is good and where it's bad. Pay attention to that, because it will help point to a problem.
another good thing about Wikipedia pages is that it often contains a lot of citations, which are good starting points to mine for useful information about the solution.
remember: always keep in mind we're searching for the problem, and that we're searching for the exact sub-problem this solution is addressing. We're interested in using this solution as a wedge to pry open this problem domain and see what manual problems developers face here.
