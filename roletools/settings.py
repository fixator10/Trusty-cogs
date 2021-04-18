import logging

from typing import Optional
from redbot.core import commands, bank
from redbot.core.i18n import Translator
from redbot.core.commands import Context

from .converter import RoleHierarchyConverter
from .abc import RoleToolsMixin, roletools

log = logging.getLogger("red.Trusty-cogs.RoleTools")
_ = Translator("RoleTools", __file__)


class RoleToolsSettings(RoleToolsMixin):
    """This class handles setting the roletools role settings."""

    @roletools.command()
    @commands.admin_or_permissions(manage_roles=True)
    async def selfadd(
        self, ctx: Context, true_or_false: Optional[bool] = None, *, role: RoleHierarchyConverter
    ) -> None:
        """
        Set whether or not a user can apply the role to themselves.

        `[true_or_false]` optional boolean of what to set the setting to.
        If not provided the current setting will be shown instead.
        `<role>` The role you want to set.
        """
        cur_setting = await self.config.role(role).selfassignable()
        if true_or_false is None:
            if cur_setting:
                await ctx.send(_("The {role} role is self assignable.").format(role=role))
            else:
                command = f"`{ctx.clean_prefix}roletools selfadd yes {role.name}`"
                await ctx.send(
                    _(
                        "The {role} role is not self assignable. Run the command "
                        "{command} to make it self assignable."
                    ).format(role=role.mention, command=command)
                )
            return
        if true_or_false is True:
            await self.config.role(role).selfassignable.set(True)
            await ctx.send(_("The {role} role is now self assignable.").format(role=role.mention))
        if true_or_false is False:
            await self.config.role(role).selfassignable.set(False)
            await ctx.send(
                _("The {role} role is no longer self assignable.").format(role=role.mention)
            )

    @roletools.command()
    @commands.admin_or_permissions(manage_roles=True)
    async def selfrem(
        self, ctx: Context, true_or_false: Optional[bool] = None, *, role: RoleHierarchyConverter
    ) -> None:
        """
        Set whether or not a user can remove the role from themselves.

        `[true_or_false]` optional boolean of what to set the setting to.
        If not provided the current setting will be shown instead.
        `<role>` The role you want to set.
        """
        cur_setting = await self.config.role(role).selfremovable()
        if true_or_false is None:
            if cur_setting:
                await ctx.send(_("The {role} role is self removeable.").format(role=role.mention))
            else:
                command = f"`{ctx.clean_prefix}roletools selfrem yes {role.name}`"
                await ctx.send(
                    _(
                        "The {role} role is not self removable. Run the command "
                        "{command} to make it self removeable."
                    ).format(role=role.mention, command=command)
                )
            return
        if true_or_false is True:
            await self.config.role(role).selfremovable.set(True)
            await ctx.send(_("The {role} role is now self removeable.").format(role=role.mention))
        if true_or_false is False:
            await self.config.role(role).selfremovable.set(False)
            await ctx.send(
                _("The {role} role is no longer self removeable.").format(role=role.mention)
            )

    @roletools.command()
    @commands.admin_or_permissions(manage_roles=True)
    async def cost(
        self, ctx: Context, cost: Optional[int] = None, *, role: RoleHierarchyConverter
    ) -> None:
        """
        Set whether or not a user can remove the role from themselves.

        `[cost]` The price you want to set the role at in bot credits.
        Setting this to 0 or lower will remove the cost.
        If not provided the current setting will be shown instead.
        `<role>` The role you want to set.
        """
        if await bank.is_global() and not await self.bot.is_owner(ctx.author):
            await ctx.send(
                _("This command is locked to bot owner only while the bank is set to global.")
            )
            return
        if cost is not None and cost >= await bank.get_max_balance(ctx.guild):
            await ctx.send(_("You cannot set a cost higher than the maximum credits balance."))
            return

        cur_setting = await self.config.role(role).cost()
        currency_name = await bank.get_currency_name(ctx.guild)
        if cost is None:
            if cur_setting:
                await ctx.send(
                    _("The role {role} currently costs {cost} {currency_name}.").format(
                        role=role, cost=cost, currency_name=currency_name
                    )
                )
            else:
                command = f"`{ctx.clean_prefix} roletools cost SOME_NUMBER {role.name}`"
                await ctx.send(
                    _(
                        "The role {role} does not currently cost any {currency_name}. "
                        "Run the command {command} to allow this role to require credits."
                    ).format(role=role.mention, command=command, currency_name=currency_name)
                )
            return
        else:
            if cost <= 0:
                await self.config.role(role).cost.clear()
                await ctx.send(
                    _("The {role} will not require any {currency_name} to acquire.").format(
                        role=role.mention, currency_name=currency_name
                    )
                )
                return
            else:
                await self.config.role(role).cost.set(cost)
                await ctx.send(
                    _("The {role} will now cost {cost} {currency_name} to acquire.").format(
                        role=role.mention, cost=cost, currency_name=currency_name
                    )
                )

    @roletools.command()
    @commands.admin_or_permissions(manage_roles=True)
    async def sticky(
        self, ctx: Context, true_or_false: Optional[bool] = None, *, role: RoleHierarchyConverter
    ) -> None:
        """
        Set whether or not a role will be re-applied when a user leaves and rejoins the server.

        `[true_or_false]` optional boolean of what to set the setting to.
        If not provided the current setting will be shown instead.
        `<role>` The role you want to set.
        """
        cur_setting = await self.config.role(role).sticky()
        if true_or_false is None:
            if cur_setting:
                await ctx.send(_("The {role} role is sticky.").format(role=role.mention))
            else:
                command = f"{ctx.clean_prefix}roletools sticky yes {role.name}"
                await ctx.send(
                    _(
                        "The {role} role is not sticky. Run the command "
                        "{command} to make it sticky."
                    ).format(role=role.mention, command=command)
                )
            return
        if true_or_false is True:
            await self.config.role(role).sticky.set(True)
            await ctx.send(_("The {role} role is now sticky.").format(role=role.mention))
        if true_or_false is False:
            await self.config.role(role).sticky.set(False)
            await ctx.send(_("The {role} role is no longer sticky.").format(role=role.mention))

    @roletools.command(aliases=["auto"])
    @commands.admin_or_permissions(manage_roles=True)
    async def autorole(
        self, ctx: Context, true_or_false: Optional[bool] = None, *, role: RoleHierarchyConverter
    ) -> None:
        """
        Set a role to be automatically applied when a user joins the server.

        `[true_or_false]` optional boolean of what to set the setting to.
        If not provided the current setting will be shown instead.
        `<role>` The role you want to set.
        """
        cur_setting = await self.config.role(role).auto()
        if true_or_false is None:
            if cur_setting:
                await ctx.send(
                    _("The role {role} is automatically applied on joining.").format(role=role)
                )
            else:
                command = f"`{ctx.clean_prefix}roletools auto yes {role.name}`"
                await ctx.send(
                    _(
                        "The {role} role is not automatically applied "
                        "when a member joins  this server. Run the command "
                        "{command} to make it automatically apply when a user joins."
                    ).format(role=role.mention, command=command)
                )
            return
        if true_or_false is True:
            async with self.config.guild(ctx.guild).auto_roles() as current_roles:
                if role.id not in current_roles:
                    current_roles.append(role.id)
                if ctx.guild.id not in self.settings:
                    self.settings[ctx.guild.id] = await self.config.guild(ctx.guild).all()
                if role.id not in self.settings[ctx.guild.id]["auto_roles"]:
                    self.settings[ctx.guild.id]["auto_roles"].append(role.id)
            await self.config.role(role).auto.set(True)
            await ctx.send(
                _("The {role} role will now automatically be applied when a user joins.").format(
                    role=role.mention
                )
            )
        if true_or_false is False:
            async with self.config.guild(ctx.guild).auto_roles() as current_roles:
                if role.id in current_roles:
                    current_roles.remove(role.id)
                if (
                    ctx.guild.id in self.settings
                    and role.id in self.settings[ctx.guild.id]["auto_roles"]
                ):
                    self.settings[ctx.guild.id]["auto_roles"].remove(role.id)
            await self.config.role(role).auto.set(False)
            await ctx.send(
                _("The {role} role will not automatically be applied when a user joins.").format(
                    role=role.mention
                )
            )