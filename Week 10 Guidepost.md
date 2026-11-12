Project Proposal
Hands on deployment of model project(s)

Project Proposal

The purpose of the project proposal is to align communication between team members and mentor. This is an internal document, so there's no need to add extra fluff or exposition. Make sure it is clear. The project proposal template includes implementation details, but this isn't mandatory at this stage. If you have implementation details, that's fine. But the most important thing is to have a clear use case, model project(s), and understanding of the problem domain. By the end of this week, you should start to feel like a Subject Matter Expert in the problem domain. As for the Project Proposal deliverable: keep it around 1 page, no more than 2. If it's longer than 2 pages, make sure it is clear all the way through.

Hands on Deployment

Many model projects have an "open core" business model, where there's a core open source component along with a paid hosted offering with more features (eg, gitlab or wordpress, etc). If yours falls into this category, then work through deploying the open source core by following the manual instructions. By default deploy to AWS.

If your model project doesn't have an open source component, it may sometimes be suitable to deploy an open source competitor. If that's not possible, because there is no open source competitor or it's just too complex, then you'll have to login and use the hosted product.

Whether you self-host an open source project or use an already-hosted product, it's very likely you'll have to integrate it with one of your own projects. To start, wire it up with a project on your laptop. Eventually, you'll have to deploy your own project live so you have an end-to-end production system that's integrated with your model project.

In some cases, you will want to do this end-to-end integration with more than 1 model project in order to gain a deeper sense of the problem domain. All of this is in service of trying to become true Subject Matter Experts.

Implementation

While the primary goal of this week is not to figure out how to implement your Capstone project, it's a good time to at least start thinking about implementation. There are two major implementation work items in a typical Capstone Project:

automating deployment (usually of the open source core)
building the extra features beyond the core open source version

If your model project isn't "open core", then you are still doing both, but you'll have to figure it out without a core open source component.

The "Hands on Deployment" work is meant to help with 1, so you feel the painpoint of manually deploying the product.

As for 2, we suggest this approach:

first, take inventory of the features in the product that's relevant to your use case (eg, some products are quite large and contain many features, but for us we're just interested in the subset that serves our use case)
next, consider features in your model project's competitors
create a "spec sheet" listing all the features for your use case

As implementation becomes easier, the major work item becomes figuring out what to implement. Capstone has always focused on the problem, but now more than ever what we build needs to be centered. This "spec sheet" is meant to help you discover what needs to be built for your use case.

For this week, this is enough. If your mind wanders into "how will we build it?", I suggest creating a column in your spec sheet to track difficulty and that's it for now. Next week, we can spend time spiking to determine actual difficulty.

Building Features for Open Core

If you're working with an "open core" model project, it can be confusing on how you approach implementation. At a high level, there are two approaches:

modify the open source codebase
service oriented architecture

There are pros/cons to either approach. In the first option, making direct changes to the codebase feels easier and more direct. However, it may make upgrading or applying patches difficult. For the second option, it makes integration and deployment more complicated. There's no perfect answer and you'll have to dive deeper in your understanding of the project to determine what the appropriate approach is.

Extra Tips

if your model project is not "open core", you'll have to find a way into implementation via research. This can be difficult, but you can also now ask AI to take a shot at a prototype based on your use case and spec sheet. If you do this, make sure you fully understand the implementation approach the AI chose. And you may even ask AI to implement it via several different ways so you can compare/contrast all the various approaches.
no more slides or even docs. Try to use demos and prototypes during meetings. If you must, docs are ok but keep them technical. Don't be too salesy or persuasive. More specificity and opinions need to come out at this stage.
do less individual work and try to keep work team-oriented. Divide and conquer may be ok, but make sure to allocate time to combine the work.
every team's pacing will start to drift at this point, so take these Guidepost docs as general guidance. If your team is ready to move ahead, that's ok! Even if your team is slightly behind, that's usually also not a big deal. Just don't fall too far behind. Make sure to work with your mentor on the pacing.
